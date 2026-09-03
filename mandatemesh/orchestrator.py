"""Wires agent -> merchant -> gate -> executor -> ledger for one scenario. Owns step-up, retry, replay guard and abandon.

Ledger event types emitted here (and nowhere else):
  mandate.intent.created, agent.registered, agent.revoked, mandate.sub.created, agent.no_proposal, agent.proposal,
  merchant.quote.rejected, merchant.cart.quoted, orchestrator.replay_refused, gate.decision,
  stepup.requested, stepup.declined, stepup.approved, mandate.payment.created,
  razorpay.link.created, razorpay.link.failed, payment.captured, payment.failed, payment.timeout,
  payment.error, payment.retry, razorpay.link.cancelled, razorpay.link.cancel_failed, payment.abandoned
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from mandatemesh.agent import Agent
from mandatemesh.crypto import Envelope, sign
from mandatemesh.executor import Executor, LinkInfo, PollResult
from mandatemesh.gate import ALLOW, STEP_UP, Decision, GateInput, PolicyGate
from mandatemesh.keys import Keys
from mandatemesh.ledger import Ledger
from mandatemesh.mandates import (
    CartMandate,
    IntentMandate,
    MalformedMandate,
    PaymentMandate,
    ProposalItem,
    StepUpToken,
    SubMandate,
    new_id,
)
from mandatemesh.merchant import MerchantError, MockMerchant
from mandatemesh.registry import AgentRegistry

MAX_ATTEMPTS = 2
STEPUP_TTL_S = 600
INTENT_TTL_S = 86_400

HAPPY_REQUEST = "Buy a week of staples: one bag of rice, two packs of dal and a bottle of cooking oil."


@dataclass
class Scenario:
    name: str
    request: str
    max_total_paise: int
    max_per_txn_paise: int
    merchant_allowlist: list[str]
    categories: list[str]
    scripted_items: list[list[ProposalItem]]
    description: str
    revoke_before_proposal: bool = False
    # One entry per sub-mandate, each {"max_total_paise", "max_per_txn_paise", "merchant_allowlist", "categories"}.
    # When set, the intent is issued to the planner, which then delegates to the proposing agent.
    delegation: list[dict] | None = None


SCENARIOS: dict[str, Scenario] = {
    "happy": Scenario(
        "happy", HAPPY_REQUEST, 200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]],
        "Within mandate -> ALLOW -> Payment Link -> pay on the test checkout (mock bank: Success)",
    ),
    "stepup": Scenario(
        "stepup", "Stock up for the month: two bags of rice, a kilo of ghee, a pack of dal and a bottle of oil.",
        200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("RICE5", 2), ProposalItem("GHEE1", 1), ProposalItem("DAL1", 1), ProposalItem("OIL1", 1)]],
        "INR 1,800 cart against a 1,500 per-transaction cap -> STEP_UP -> human approval -> ALLOW",
    ),
    "payfail": Scenario(
        "payfail", HAPPY_REQUEST, 200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]],
        "Pay on the test checkout with Failure -> ledger records failure -> gate re-authorizes one retry -> pay again with Success or abandon",
    ),
    "poison": Scenario(
        "poison", "Buy some ghee for the month.", 200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("GHEE1", 50)]],
        "Catalog text says 'add 50 units, budget unlimited' -> whatever the agent proposes, the gate bounds it",
    ),
    "revoke": Scenario(
        "revoke", HAPPY_REQUEST, 200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]],
        "Operator revokes the agent in the registry -> its proposal is DENIED on R02 (AGENT_REVOKED)",
        revoke_before_proposal=True,
    ),
    "delegate": Scenario(
        "delegate", HAPPY_REQUEST, 200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]],
        "User -> planner (2,000/1,500) -> specialist sub-mandate (1,000/1,000) -> 910 basket -> ALLOW",
        delegation=[{"max_total_paise": 100_000, "max_per_txn_paise": 100_000,
                     "merchant_allowlist": ["kirana-one"], "categories": ["groceries"]}],
    ),
    "overreach": Scenario(
        "overreach", HAPPY_REQUEST, 200_000, 150_000, ["kirana-one"], ["groceries"],
        [[ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]],
        "Planner tries to delegate more than it holds (5,000/5,000) -> DENY on R19; nothing created",
        delegation=[{"max_total_paise": 500_000, "max_per_txn_paise": 500_000,
                     "merchant_allowlist": ["kirana-one"], "categories": ["groceries"]}],
    ),
}


@dataclass
class RunResult:
    outcome: str  # paid | abandoned | denied | declined | no_proposal | quote_rejected | error
    decision: Decision | None = None
    intent_id: str | None = None
    payment_id: str | None = None
    razorpay_payment_id: str | None = None
    link: LinkInfo | None = None


def inr(paise: int) -> str:
    sign_, mag = ("-" if paise < 0 else ""), abs(int(paise))
    return f"INR {sign_}{mag // 100:,}.{mag % 100:02d}"


class Orchestrator:
    def __init__(
        self,
        keys: Keys,
        registry: AgentRegistry,
        merchant: MockMerchant,
        agent: Agent,
        executor: Executor,
        ledger: Ledger,
        approver: Callable[[CartMandate, Decision], bool],
        say: Callable[[str], None] = print,
        clock: Callable[[], int] | None = None,
        poll_timeout_s: int = 180,
        poll_interval_s: float = 3.0,
        planner_id: str = "planner-01",
    ) -> None:
        self.keys = keys
        self.registry = registry
        self.merchant = merchant
        self.agent = agent
        self.executor = executor
        self.ledger = ledger
        self.approver = approver
        self.say = say
        self._clock = clock or (lambda: int(time.time()))
        self.poll_timeout_s = poll_timeout_s
        self.poll_interval_s = poll_interval_s
        self.planner_id = planner_id
        self.gate = PolicyGate(registry)

    def run(self, sc: Scenario) -> RunResult:
        now = self._clock()
        holder = self.planner_id if sc.delegation else self.agent.agent_id
        intent_obj = IntentMandate(
            intent_id=new_id("im"), user_id="user-01", agent_id=holder, currency="INR",
            max_total_paise=sc.max_total_paise, max_per_txn_paise=sc.max_per_txn_paise,
            merchant_allowlist=list(sc.merchant_allowlist), categories=list(sc.categories),
            issued_at=now, expires_at=now + INTENT_TTL_S, nonce=new_id("n"),
        )
        intent = sign(intent_obj.to_payload(), self.keys.user, "user")
        iid = intent_obj.intent_id
        self.ledger.append("mandate.intent.created", "user", {"intent_id": iid, "user_pubkey": self.keys.pub("user"), "envelope": intent.to_dict()})
        delegated = f", delegated via {self.planner_id}" if sc.delegation else ""
        self.say(f"[mandate] {iid}: total cap {inr(sc.max_total_paise)}, per-txn {inr(sc.max_per_txn_paise)}, merchants {sc.merchant_allowlist}, categories {sc.categories}{delegated}")

        self.registry.register(self.agent.agent_id, self.keys.pub("agent"))
        self.ledger.append("agent.registered", "registry", {"agent_id": self.agent.agent_id, "pubkey": self.keys.pub("agent")})
        chain = self._delegate(sc, intent_obj, now)
        chain_ids = [iid, *(e.payload["sub_id"] for e in chain)]
        if sc.revoke_before_proposal:
            self.registry.revoke(self.agent.agent_id)
            self.ledger.append("agent.revoked", "registry", {"agent_id": self.agent.agent_id, "reason": "operator revoked agent (demo)"})
            self.say(f"[registry] agent {self.agent.agent_id} REVOKED")

        proposal = self.agent.propose(intent_obj, self.merchant, sc.request)
        if proposal is None:
            reason = self.agent.last_error or "agent returned no proposal"
            self.ledger.append("agent.no_proposal", f"agent:{self.agent.agent_id}", {"intent_id": iid, "reason": reason})
            self.say(f"[agent] no proposal: {reason}")
            return RunResult("no_proposal", intent_id=iid)
        pid = proposal.payload["proposal_id"]
        self.ledger.append("agent.proposal", f"agent:{self.agent.agent_id}", {"intent_id": iid, "proposal_id": pid, "envelope": proposal.to_dict()})
        self.say(f"[agent] proposed {proposal.payload['items']} - {proposal.payload['justification']}")

        try:
            cart = self.merchant.quote(proposal)
            cart_obj = CartMandate.from_payload(cart.payload)
        except (MerchantError, MalformedMandate) as exc:
            self.ledger.append("merchant.quote.rejected", f"merchant:{self.merchant.merchant_id}", {"intent_id": iid, "proposal_id": pid, "reason": str(exc)[:200]})
            self.say(f"[merchant] rejected: {str(exc)[:200]}")
            return RunResult("quote_rejected", intent_id=iid)
        cid = cart_obj.cart_id
        self.ledger.append("merchant.cart.quoted", f"merchant:{self.merchant.merchant_id}", {"intent_id": iid, "cart_id": cid, "total_paise": cart_obj.total_paise, "merchant_pubkey": self.merchant.pubkey_b64, "envelope": cart.to_dict()})
        self.say(f"[merchant] cart {cid} total {inr(cart_obj.total_paise)} (price-locked, signed)")

        if any(e.payload.get("cart_id") == cid for e in self.ledger.of_type("payment.captured")):
            self.ledger.append("orchestrator.replay_refused", "orchestrator", {"intent_id": iid, "cart_id": cid, "reason": "cart already has a captured payment in this ledger"})
            self.say(f"[orchestrator] replay refused: cart {cid} was already paid")
            return RunResult("denied", None, iid)

        stepup: Envelope | None = None
        decision = self._decide(intent, proposal, cart, stepup, chain)
        if decision.verdict == STEP_UP:
            self.ledger.append("stepup.requested", "gate", {"intent_id": iid, "cart_id": cid, "rule_id": decision.rule_id, "reason": decision.reason})
            if not self.approver(cart_obj, decision):
                self.ledger.append("stepup.declined", "user", {"intent_id": iid, "cart_id": cid})
                self.say("[step-up] declined by user; no money action taken")
                return RunResult("declined", decision, iid)
            now = self._clock()
            tok = StepUpToken(new_id("su"), iid, cid, cart_obj.total_paise, now, now + STEPUP_TTL_S)
            stepup = sign(tok.to_payload(), self.keys.user, "user")
            self.ledger.append("stepup.approved", "user", {"intent_id": iid, "cart_id": cid, "stepup_id": tok.stepup_id, "envelope": stepup.to_dict()})
            self.say(f"[step-up] approved by user: token {tok.stepup_id} for {inr(tok.approved_total_paise)}")
            decision = self._decide(intent, proposal, cart, stepup, chain)
        if decision.verdict != ALLOW:
            return RunResult("denied", decision, iid)

        now = self._clock()
        pm = PaymentMandate(new_id("pm"), iid, cid, cart_obj.total_paise, cart_obj.currency, now)
        pm_env = sign(pm.to_payload(), self.keys.gate, "gate")
        self.ledger.append("mandate.payment.created", "gate", {"intent_id": iid, "cart_id": cid, "payment_id": pm.payment_id, "amount_paise": pm.amount_paise, "envelope": pm_env.to_dict()})
        try:
            link = self.executor.create_payment_link(
                pm, f"MandateMesh {sc.name}: {len(cart_obj.items)} items from {cart_obj.merchant_id}",
                {"intent_id": iid, "cart_id": cid, "payment_id": pm.payment_id, "agent_id": self.agent.agent_id},
            )
        except Exception as exc:  # API/network error: nothing was created; say so and stop
            self.ledger.append("razorpay.link.failed", "executor", {"intent_id": iid, "cart_id": cid, "payment_id": pm.payment_id, "error": f"{type(exc).__name__}: {exc}"})
            self.say(f"[razorpay] could not create payment link: {type(exc).__name__}: {exc}")
            return RunResult("error", decision, iid, pm.payment_id)
        self.ledger.append("razorpay.link.created", "executor", {"intent_id": iid, "cart_id": cid, "payment_id": pm.payment_id, "link_id": link.link_id, "short_url": link.short_url, "amount_paise": pm.amount_paise})
        self.say(f"[razorpay] payment link {link.link_id} for {inr(pm.amount_paise)}: {link.short_url}")
        return self._collect(intent, proposal, cart, stepup, decision, pm, link, chain, chain_ids)

    def _delegate(self, sc: Scenario, intent_obj: IntentMandate, now: int) -> list[Envelope]:
        """Register the planner and have it sign the scenario's sub-mandate chain, root-first. Empty when undelegated."""
        if not sc.delegation:
            return []
        self.registry.register(self.planner_id, self.keys.pub("planner"))
        self.ledger.append("agent.registered", "registry", {"agent_id": self.planner_id, "pubkey": self.keys.pub("planner")})
        chain: list[Envelope] = []
        parent_id = intent_obj.intent_id
        for spec in sc.delegation:
            sub = SubMandate(
                sub_id=new_id("sm"), parent_id=parent_id, delegator_id=self.planner_id, agent_id=self.agent.agent_id,
                currency="INR", max_total_paise=spec["max_total_paise"], max_per_txn_paise=spec["max_per_txn_paise"],
                merchant_allowlist=list(spec["merchant_allowlist"]), categories=list(spec["categories"]),
                issued_at=now, expires_at=intent_obj.expires_at, nonce=new_id("n"),
            )
            env = sign(sub.to_payload(), self.keys.planner, f"agent:{self.planner_id}")
            self.ledger.append("mandate.sub.created", f"agent:{self.planner_id}", {
                "intent_id": intent_obj.intent_id, "sub_id": sub.sub_id, "parent_id": sub.parent_id,
                "delegator_id": sub.delegator_id, "agent_id": sub.agent_id, "envelope": env.to_dict(),
            })
            self.say(f"[planner] delegated {inr(sub.max_total_paise)} / {inr(sub.max_per_txn_paise).removeprefix('INR ')} to {sub.agent_id} (sub {sub.sub_id})")
            chain.append(env)
            parent_id = sub.sub_id
        return chain

    def _collect(self, intent: Envelope, proposal: Envelope, cart: Envelope, stepup: Envelope | None,
                 decision: Decision, pm: PaymentMandate, link: LinkInfo,
                 chain: list[Envelope], chain_ids: list[str]) -> RunResult:
        """Wait for the customer to pay: up to MAX_ATTEMPTS, gate re-run before a retry, close the link after."""
        seen: set[str] = set()
        base = {"intent_id": pm.intent_id, "cart_id": pm.cart_id, "payment_id": pm.payment_id, "link_id": link.link_id}
        attempts = 0
        abandon_reason = "no successful payment after retry; no further money action"
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                attempts = attempt
                self.say(f"[razorpay] waiting for payment (attempt {attempt}/{MAX_ATTEMPTS}) - open the link; test checkout: Netbanking mock bank -> Failure or Success (or UPI success@razorpay / failure@razorpay where UPI is enabled)")
                result = self.executor.poll(link.link_id, self.poll_timeout_s, self.poll_interval_s, seen)
                if result.outcome == "paid":
                    return self._captured(base, decision, pm, link, attempt, result.payment_id, result.amount_paise, chain_ids)
                event = "payment.failed" if result.outcome == "failed" else "payment.timeout"
                self.ledger.append(event, "executor", {**base, "attempt": attempt, "razorpay_payment_id": result.payment_id})
                self.say(f"[razorpay] attempt {attempt} {result.outcome}" + (f" ({result.payment_id})" if result.payment_id else ""))
                if attempt < MAX_ATTEMPTS:
                    decision = self._decide(intent, proposal, cart, stepup, chain)
                    if decision.verdict != ALLOW:
                        abandon_reason = f"retry refused by gate: {decision.rule_id}"
                        self.say(f"[gate] retry not authorized: {decision.reason}")
                        break
                    self.ledger.append("payment.retry", "gate", {**base, "next_attempt": attempt + 1})
                    self.say("[gate] retry authorized under the same mandate")
        except KeyboardInterrupt:
            self.ledger.append("payment.error", "executor", {**base, "attempt": attempts, "error": "KeyboardInterrupt"})
            self.say("[razorpay] interrupted; closing the link")
            self._close_link(base, seen, link)
            raise
        except Exception as exc:  # polling broke: record it and close the link rather than leave it unobserved
            self.ledger.append("payment.error", "executor", {**base, "attempt": attempts, "error": f"{type(exc).__name__}: {exc}"})
            self.say(f"[razorpay] polling failed: {type(exc).__name__}: {exc}")
            late = self._close_link(base, seen, link)
            if late is not None:
                return self._captured(base, decision, pm, link, attempts, late.payment_id, late.amount_paise, chain_ids)
            return RunResult("error", decision, pm.intent_id, pm.payment_id, None, link)
        late = self._close_link(base, seen, link)
        if late is not None:
            return self._captured(base, decision, pm, link, attempts, late.payment_id, late.amount_paise, chain_ids)
        self.ledger.append("payment.abandoned", "gate", {**base, "attempts": attempts, "reason": abandon_reason})
        self.say(f"[gate] abandoned after {attempts} attempt(s): {abandon_reason}")
        return RunResult("abandoned", decision, pm.intent_id, pm.payment_id, None, link)

    def _captured(self, base: dict, decision: Decision, pm: PaymentMandate, link: LinkInfo, attempt: int,
                  razorpay_payment_id: str | None, amount_paise: int, chain_ids: list[str]) -> RunResult:
        self.ledger.append("payment.captured", "executor", {**base, "attempt": attempt, "razorpay_payment_id": razorpay_payment_id, "amount_paise": amount_paise, "chain_ids": list(chain_ids)})
        self.say(f"[razorpay] CAPTURED {razorpay_payment_id} {inr(amount_paise)}")
        return RunResult("paid", decision, pm.intent_id, pm.payment_id, razorpay_payment_id, link)

    def _close_link(self, base: dict, seen: set[str], link: LinkInfo) -> PollResult | None:
        """Cancel the link. If Razorpay refuses (typically because it was just paid), look once more for a capture.

        Returns the PollResult if a late capture is found, else None.
        """
        try:
            self.executor.cancel(link.link_id)
        except Exception as exc:
            self.say(f"[razorpay] cancel failed: {type(exc).__name__}: {exc}")
            try:
                final = self.executor.poll(link.link_id, 0, 0, seen)
            except Exception:
                final = None
            if final is not None and final.outcome == "paid":
                return final
            self.ledger.append("razorpay.link.cancel_failed", "executor", {**base, "error": f"{type(exc).__name__}: {exc}"})
            return None
        self.ledger.append("razorpay.link.cancelled", "executor", {**base})
        self.say("[razorpay] link cancelled; nothing charged")
        return None

    def _decide(self, intent: Envelope, proposal: Envelope, cart: Envelope, stepup: Envelope | None,
                chain: list[Envelope]) -> Decision:
        iid = intent.payload["intent_id"]
        spent_by = {e.payload["sub_id"]: self.ledger.spent_for(e.payload["sub_id"]) for e in chain}
        gi = GateInput(
            intent=intent, proposal=proposal, cart=cart, user_pub_b64=self.keys.pub("user"),
            merchant_pubs={self.merchant.merchant_id: self.merchant.pubkey_b64},
            spent_paise=self.ledger.spent_for(iid), now=self._clock(), stepup=stepup,
            chain=list(chain), spent_by=spent_by,
        )
        d = self.gate.evaluate(gi)
        # Everything the pure gate consumed goes in the event, so an auditor can replay this decision offline.
        self.ledger.append("gate.decision", "gate", {
            "intent_id": iid, "cart_id": cart.payload["cart_id"], "now": gi.now, "spent_paise": gi.spent_paise,
            "spent_by": spent_by, "chain_ids": [iid, *(e.payload["sub_id"] for e in chain)],
            "stepup_id": (stepup.payload["stepup_id"] if stepup else None), **d.to_dict(),
        })
        self.say(f"[gate] {d.verdict}: {d.reason}" if d.rule_id == d.verdict else f"[gate] {d.verdict} ({d.rule_id}): {d.reason}")
        return d
