"""Deterministic policy gate. Pure: no I/O, no clock, no LLM, no network. First failing rule decides."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from mandatemesh.crypto import Envelope, verify
from mandatemesh.mandates import AgentProposal, CartMandate, IntentMandate, MalformedMandate, StepUpToken
from mandatemesh.registry import ACTIVE, AgentRegistry

ALLOW = "ALLOW"
DENY = "DENY"
STEP_UP = "STEP_UP"


@dataclass
class Check:
    rule_id: str
    passed: bool
    detail: str


@dataclass
class Decision:
    verdict: str
    rule_id: str
    reason: str
    checks: list[Check] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class GateInput:
    intent: Envelope
    proposal: Envelope
    cart: Envelope
    user_pub_b64: str
    merchant_pubs: dict[str, str]
    spent_paise: int
    now: int
    stepup: Envelope | None = None


def rupees(paise: int) -> str:
    return f"INR {paise / 100:,.2f}"


class PolicyGate:
    def __init__(self, registry: AgentRegistry) -> None:
        self.registry = registry

    def evaluate(self, gi: GateInput) -> Decision:
        checks: list[Check] = []

        def ok(rule: str, detail: str) -> None:
            checks.append(Check(rule, True, detail))

        def fail(rule: str, detail: str, verdict: str = DENY) -> Decision:
            checks.append(Check(rule, False, detail))
            return Decision(verdict, rule, detail, checks)

        try:
            proposal = AgentProposal.from_payload(gi.proposal.payload)
            intent = IntentMandate.from_payload(gi.intent.payload)
            cart = CartMandate.from_payload(gi.cart.payload)
        except MalformedMandate as exc:
            return fail("R00_WELL_FORMED", f"malformed mandate: {exc}")
        ok("R00_WELL_FORMED", "intent, proposal and cart payloads are well-formed")

        rec = self.registry.get(proposal.agent_id)
        if rec is None:
            return fail("R01_AGENT_REGISTERED", f"agent '{proposal.agent_id}' is not in the trusted-agent registry")
        ok("R01_AGENT_REGISTERED", f"agent '{proposal.agent_id}' is registered")

        if rec.status != ACTIVE:
            return fail("R02_AGENT_ACTIVE", f"AGENT_REVOKED: agent '{proposal.agent_id}' status is '{rec.status}'")
        ok("R02_AGENT_ACTIVE", "agent status is active")

        if not verify(gi.proposal, rec.pubkey_b64):
            return fail("R03_PROPOSAL_SIG", "proposal signature does not verify against the registry key")
        ok("R03_PROPOSAL_SIG", "proposal signature verified against registry key")

        if not verify(gi.intent, gi.user_pub_b64):
            return fail("R04_INTENT_SIG", "intent mandate signature does not verify against the user key")
        ok("R04_INTENT_SIG", "intent mandate signature verified")

        if gi.now >= intent.expires_at:
            return fail("R05_INTENT_NOT_EXPIRED", f"intent expired at {intent.expires_at}; now is {gi.now}")
        ok("R05_INTENT_NOT_EXPIRED", f"intent valid until {intent.expires_at}")

        if proposal.agent_id != intent.agent_id:
            return fail("R06_INTENT_AGENT_MATCH", f"intent delegates to '{intent.agent_id}' but proposal is from '{proposal.agent_id}'")
        ok("R06_INTENT_AGENT_MATCH", "proposal comes from the delegated agent")

        merchant_pub = gi.merchant_pubs.get(cart.merchant_id)
        if merchant_pub is None or not verify(gi.cart, merchant_pub):
            return fail("R07_CART_SIG", f"cart signature does not verify for merchant '{cart.merchant_id}'")
        ok("R07_CART_SIG", f"cart mandate signature verified for merchant '{cart.merchant_id}'")

        if cart.intent_id != intent.intent_id or cart.proposal_id != proposal.proposal_id:
            return fail("R08_CART_CHAIN", "cart does not reference this intent and proposal")
        ok("R08_CART_CHAIN", "cart references this intent and this proposal")

        if gi.now >= cart.expires_at:
            return fail("R09_CART_NOT_EXPIRED", f"cart quote expired at {cart.expires_at}; now is {gi.now}")
        ok("R09_CART_NOT_EXPIRED", f"cart quote valid until {cart.expires_at}")

        computed = sum(i.qty * i.unit_price_paise for i in cart.items)
        if computed != cart.total_paise:
            return fail("R10_CART_TOTAL_INTEGRITY", f"cart total {cart.total_paise} != sum of lines {computed}")
        ok("R10_CART_TOTAL_INTEGRITY", f"cart total {rupees(cart.total_paise)} equals the sum of its lines")

        if sorted((i.sku, i.qty) for i in cart.items) != sorted((i.sku, i.qty) for i in proposal.items):
            return fail("R11_CART_MATCHES_PROPOSAL", "cart items differ from what the agent proposed")
        ok("R11_CART_MATCHES_PROPOSAL", "cart items match the agent's proposal")

        if cart.merchant_id not in intent.merchant_allowlist:
            return fail("R12_MERCHANT_ALLOWED", f"merchant '{cart.merchant_id}' is not in the allow-list {intent.merchant_allowlist}")
        ok("R12_MERCHANT_ALLOWED", f"merchant '{cart.merchant_id}' is allow-listed")

        bad = sorted({i.category for i in cart.items} - set(intent.categories))
        if bad:
            return fail("R13_CATEGORY_ALLOWED", f"categories {bad} are not permitted by the mandate {intent.categories}")
        ok("R13_CATEGORY_ALLOWED", "all item categories are permitted")

        if cart.currency != intent.currency:
            return fail("R17_CURRENCY_MATCH", f"cart currency {cart.currency} != mandate currency {intent.currency}")
        ok("R17_CURRENCY_MATCH", f"currency {cart.currency}")

        stepup_ok: bool | None = None
        stepup_detail = "no step-up token supplied"
        if gi.stepup is not None:
            stepup_ok, stepup_detail = self._check_stepup(gi, intent, cart)

        if cart.total_paise > intent.max_per_txn_paise:
            detail = f"cart {rupees(cart.total_paise)} exceeds the per-transaction cap {rupees(intent.max_per_txn_paise)}"
            if gi.stepup is None:
                return fail("R14_PER_TXN_CAP", detail + "; human step-up required", STEP_UP)
            if not stepup_ok:
                return fail("R16_STEPUP_TOKEN_VALID", stepup_detail)
            ok("R14_PER_TXN_CAP", detail + "; covered by step-up approval")
        else:
            ok("R14_PER_TXN_CAP", f"cart {rupees(cart.total_paise)} is within the per-transaction cap {rupees(intent.max_per_txn_paise)}")

        projected = gi.spent_paise + cart.total_paise
        if projected > intent.max_total_paise:
            detail = f"spent {rupees(gi.spent_paise)} + cart {rupees(cart.total_paise)} exceeds the total cap {rupees(intent.max_total_paise)}"
            if gi.stepup is None:
                return fail("R15_TOTAL_CAP", detail + "; human step-up required", STEP_UP)
            if not stepup_ok:
                return fail("R16_STEPUP_TOKEN_VALID", stepup_detail)
            ok("R15_TOTAL_CAP", detail + "; covered by step-up approval")
        else:
            ok("R15_TOTAL_CAP", f"projected spend {rupees(projected)} is within the total cap {rupees(intent.max_total_paise)}")

        if gi.stepup is not None:
            if not stepup_ok:
                return fail("R16_STEPUP_TOKEN_VALID", stepup_detail)
            ok("R16_STEPUP_TOKEN_VALID", stepup_detail)

        return Decision(ALLOW, "ALLOW", f"all {len(checks)} checks passed; authorizing {rupees(cart.total_paise)} to '{cart.merchant_id}'", checks)

    def _check_stepup(self, gi: GateInput, intent: IntentMandate, cart: CartMandate) -> tuple[bool, str]:
        assert gi.stepup is not None
        if not verify(gi.stepup, gi.user_pub_b64):
            return False, "step-up token signature does not verify against the user key"
        try:
            tok = StepUpToken.from_payload(gi.stepup.payload)
        except MalformedMandate as exc:
            return False, f"step-up token malformed: {exc}"
        if tok.intent_id != intent.intent_id or tok.cart_id != cart.cart_id:
            return False, "step-up token is bound to a different intent or cart"
        if gi.now >= tok.expires_at:
            return False, f"step-up token expired at {tok.expires_at}"
        if tok.approved_total_paise < cart.total_paise:
            return False, f"step-up approved {rupees(tok.approved_total_paise)} but cart is {rupees(cart.total_paise)}"
        return True, f"step-up {tok.stepup_id} approves {rupees(tok.approved_total_paise)} for cart {cart.cart_id}"
