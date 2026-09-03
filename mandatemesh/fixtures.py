"""Shared builders for tests and the eval set. Fixed clock, in-memory keys, no network."""
from __future__ import annotations

from dataclasses import dataclass

from mandatemesh.crypto import Envelope, sign
from mandatemesh.gate import GateInput, PolicyGate
from mandatemesh.keys import Keys
from mandatemesh.mandates import AgentProposal, IntentMandate, ProposalItem, StepUpToken, SubMandate, new_id
from mandatemesh.merchant import MockMerchant
from mandatemesh.registry import AgentRegistry

FIXED_NOW = 1_800_000_000
AGENT_ID = "shopper-01"
PLANNER_ID = "planner-01"
MERCHANT_ID = "kirana-one"
HAPPY_ITEMS = [ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]  # 91,000 paise
STEPUP_ITEMS = [ProposalItem("RICE5", 2), ProposalItem("GHEE1", 1), ProposalItem("DAL1", 1), ProposalItem("OIL1", 1)]  # 180,000 paise
POISON_ITEMS = [ProposalItem("GHEE1", 50)]  # 3,000,000 paise


@dataclass
class World:
    keys: Keys
    registry: AgentRegistry
    merchant: MockMerchant
    gate: PolicyGate
    now: int


def make_world(now: int = FIXED_NOW) -> World:
    keys = Keys.generate()
    registry = AgentRegistry()
    registry.register(AGENT_ID, keys.pub("agent"))
    registry.register(PLANNER_ID, keys.pub("planner"))
    merchant = MockMerchant(MERCHANT_ID, keys.merchant, clock=lambda: now)
    return World(keys, registry, merchant, PolicyGate(registry), now)


def make_intent(w: World, **over) -> Envelope:
    fields = dict(
        intent_id=new_id("im"), user_id="user-01", agent_id=AGENT_ID, currency="INR",
        max_total_paise=200_000, max_per_txn_paise=150_000, merchant_allowlist=[MERCHANT_ID],
        categories=["groceries"], issued_at=w.now, expires_at=w.now + 86_400, nonce=new_id("n"),
    )
    fields.update(over)
    return sign(IntentMandate(**fields).to_payload(), w.keys.user, "user")


def make_proposal(w: World, intent_env: Envelope, items: list[ProposalItem] | None = None, **over) -> Envelope:
    fields = dict(
        proposal_id=new_id("ap"), agent_id=AGENT_ID, intent_id=intent_env.payload["intent_id"],
        merchant_id=MERCHANT_ID, items=list(items or HAPPY_ITEMS), justification="weekly staples", issued_at=w.now,
    )
    fields.update(over)
    return sign(AgentProposal(**fields).to_payload(), w.keys.agent, f"agent:{fields['agent_id']}")


def make_stepup(w: World, intent_env: Envelope, cart_env: Envelope, approved_total_paise: int | None = None, **over) -> Envelope:
    fields = dict(
        stepup_id=new_id("su"), intent_id=intent_env.payload["intent_id"], cart_id=cart_env.payload["cart_id"],
        approved_total_paise=cart_env.payload["total_paise"] if approved_total_paise is None else approved_total_paise,
        issued_at=w.now, expires_at=w.now + 600,
    )
    fields.update(over)
    return sign(StepUpToken(**fields).to_payload(), w.keys.user, "user")


def make_sub(w: World, parent_env: Envelope, delegator_key=None, delegator_id: str = PLANNER_ID, **over) -> Envelope:
    """A sub-mandate narrowing `parent_env` (an intent or another sub-mandate) for AGENT_ID, signed by the delegator."""
    fields = dict(
        sub_id=new_id("sm"), parent_id=parent_env.payload.get("intent_id") or parent_env.payload["sub_id"],
        delegator_id=delegator_id, agent_id=AGENT_ID, currency="INR", max_total_paise=100_000, max_per_txn_paise=100_000,
        merchant_allowlist=[MERCHANT_ID], categories=["groceries"], issued_at=w.now, expires_at=parent_env.payload["expires_at"],
        nonce=new_id("n"),
    )
    fields.update(over)
    return sign(SubMandate(**fields).to_payload(), delegator_key or w.keys.planner, f"agent:{delegator_id}")


def make_gate_input(w: World, intent_env: Envelope, proposal_env: Envelope, cart_env: Envelope,
                    spent_paise: int = 0, stepup: Envelope | None = None, now: int | None = None,
                    chain: list[Envelope] | None = None, spent_by: dict[str, int] | None = None) -> GateInput:
    return GateInput(
        intent=intent_env, proposal=proposal_env, cart=cart_env, user_pub_b64=w.keys.pub("user"),
        merchant_pubs={MERCHANT_ID: w.merchant.pubkey_b64}, spent_paise=spent_paise,
        now=w.now if now is None else now, stepup=stepup, chain=list(chain or []), spent_by=dict(spent_by or {}),
    )


def happy_chain(w: World, items: list[ProposalItem] | None = None, **intent_over) -> tuple[Envelope, Envelope, Envelope]:
    intent = make_intent(w, **intent_over)
    proposal = make_proposal(w, intent, items)
    return intent, proposal, w.merchant.quote(proposal)


def delegated_chain(w: World, items: list[ProposalItem] | None = None, **sub_over) -> tuple[Envelope, Envelope, Envelope, Envelope]:
    """user -> planner (intent) -> shopper (sub-mandate) -> proposal -> cart. Returns (intent, sub, proposal, cart)."""
    intent = make_intent(w, agent_id=PLANNER_ID)
    sub = make_sub(w, intent, **sub_over)
    proposal = make_proposal(w, intent, items)
    return intent, sub, proposal, w.merchant.quote(proposal)


def resign_cart(w: World, cart_env: Envelope, **changes) -> Envelope:
    """Merchant re-signs an altered cart (simulates a buggy or colluding merchant)."""
    payload = dict(cart_env.payload)
    payload.update(changes)
    return sign(payload, w.keys.merchant, cart_env.signer)
