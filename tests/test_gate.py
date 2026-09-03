import time

import pytest

from mandatemesh.crypto import Envelope, sign
from mandatemesh.fixtures import (
    AGENT_ID, HAPPY_ITEMS, MERCHANT_ID, PLANNER_ID, STEPUP_ITEMS, delegated_chain, happy_chain, make_gate_input, make_intent,
    make_payment_mandate, make_proposal, make_refund_input, make_shortfall, make_stepup, make_sub, make_world, resign_cart,
)
from mandatemesh.gate import ALLOW, DENY, STEP_UP, PolicyGate
from mandatemesh.mandates import AgentProposal, ProposalItem, new_id
from mandatemesh.registry import AgentRegistry


@pytest.fixture
def w():
    return make_world()


def decide(w, intent, proposal, cart, **kw):
    return w.gate.evaluate(make_gate_input(w, intent, proposal, cart, **kw))


def test_happy_path_allows_with_full_trail(w):
    d = decide(w, *happy_chain(w))
    assert d.verdict == ALLOW and d.rule_id == "ALLOW"
    assert [c.rule_id for c in d.checks][:3] == ["R00_WELL_FORMED", "R01_AGENT_REGISTERED", "R02_AGENT_ACTIVE"]
    assert d.checks[-1].rule_id == "R15_TOTAL_CAP"
    assert all(c.passed for c in d.checks)
    assert "910.00" in d.reason


def test_r00_malformed_proposal_denies(w):
    intent, proposal, cart = happy_chain(w)
    malformed = sign({**proposal.payload, "note": "extra field from a chatty model"}, w.keys.agent, proposal.signer)
    d = decide(w, intent, malformed, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R00_WELL_FORMED")
    assert "unknown keys" in d.reason


def test_r01_unregistered_agent(w):
    intent, proposal, cart = happy_chain(w)
    w.gate = PolicyGate(AgentRegistry())
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R01_AGENT_REGISTERED")


def test_r02_revoked_agent(w):
    intent, proposal, cart = happy_chain(w)
    w.registry.revoke(AGENT_ID)
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R02_AGENT_ACTIVE")
    assert "AGENT_REVOKED" in d.reason


def test_r03_forged_proposal_signature(w):
    intent, proposal, cart = happy_chain(w)
    forged = sign(proposal.payload, w.keys.user, proposal.signer)
    d = decide(w, intent, forged, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R03_PROPOSAL_SIG")


def test_r04_forged_intent_signature(w):
    intent, proposal, cart = happy_chain(w)
    forged = sign(intent.payload, w.keys.gate, "user")
    d = decide(w, forged, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R04_INTENT_SIG")


def test_r05_expired_intent(w):
    intent, proposal, cart = happy_chain(w)
    d = decide(w, intent, proposal, cart, now=intent.payload["expires_at"])
    assert (d.verdict, d.rule_id) == (DENY, "R05_INTENT_NOT_EXPIRED")


def test_r06_proposal_from_other_registered_agent(w):
    w.registry.register("other-agent", w.keys.pub("agent"))
    intent = make_intent(w)
    proposal = make_proposal(w, intent, agent_id="other-agent")
    cart = w.merchant.quote(proposal)
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R06_INTENT_AGENT_MATCH")


def test_r07_cart_signed_by_wrong_key(w):
    intent, proposal, cart = happy_chain(w)
    forged = sign(cart.payload, w.keys.user, cart.signer)
    d = decide(w, intent, proposal, forged)
    assert (d.verdict, d.rule_id) == (DENY, "R07_CART_SIG")


def test_r08_cart_references_other_intent(w):
    intent, proposal, cart = happy_chain(w)
    d = decide(w, intent, proposal, resign_cart(w, cart, intent_id="im_other"))
    assert (d.verdict, d.rule_id) == (DENY, "R08_CART_CHAIN")


def test_r09_expired_cart(w):
    intent, proposal, cart = happy_chain(w)
    d = decide(w, intent, proposal, cart, now=cart.payload["expires_at"])
    assert (d.verdict, d.rule_id) == (DENY, "R09_CART_NOT_EXPIRED")


def test_r10_tampered_total(w):
    intent, proposal, cart = happy_chain(w)
    d = decide(w, intent, proposal, resign_cart(w, cart, total_paise=cart.payload["total_paise"] - 1))
    assert (d.verdict, d.rule_id) == (DENY, "R10_CART_TOTAL_INTEGRITY")


def test_r11_merchant_altered_quantities(w):
    intent, proposal, cart = happy_chain(w)
    items = [dict(i) for i in cart.payload["items"]]
    items[0]["qty"] += 1
    total = sum(i["qty"] * i["unit_price_paise"] for i in items)
    d = decide(w, intent, proposal, resign_cart(w, cart, items=items, total_paise=total))
    assert (d.verdict, d.rule_id) == (DENY, "R11_CART_MATCHES_PROPOSAL")


def test_r12_merchant_not_allowlisted(w):
    intent, proposal, cart = happy_chain(w, merchant_allowlist=["other-shop"])
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R12_MERCHANT_ALLOWED")


def test_r13_off_category_item(w):
    intent, proposal, cart = happy_chain(w, items=[ProposalItem("MIXER", 1)])
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R13_CATEGORY_ALLOWED")
    assert "electronics" in d.reason


def test_r17_currency_mismatch(w):
    intent, proposal, cart = happy_chain(w)
    d = decide(w, intent, proposal, resign_cart(w, cart, currency="USD"))
    assert (d.verdict, d.rule_id) == (DENY, "R17_CURRENCY_MATCH")


def test_r14_per_txn_cap_requests_step_up(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (STEP_UP, "R14_PER_TXN_CAP")
    assert "1,800.00" in d.reason and "1,500.00" in d.reason


def test_r15_total_cap_requests_step_up(w):
    intent, proposal, cart = happy_chain(w)
    d = decide(w, intent, proposal, cart, spent_paise=150_000)
    assert (d.verdict, d.rule_id) == (STEP_UP, "R15_TOTAL_CAP")


def test_valid_step_up_token_allows_over_cap(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    d = decide(w, intent, proposal, cart, stepup=make_stepup(w, intent, cart))
    assert d.verdict == ALLOW
    assert any(c.rule_id == "R16_STEPUP_TOKEN_VALID" and c.passed for c in d.checks)


def test_r16_expired_step_up_token_denies(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    tok = make_stepup(w, intent, cart, expires_at=w.now)
    d = decide(w, intent, proposal, cart, stepup=tok)
    assert (d.verdict, d.rule_id) == (DENY, "R16_STEPUP_TOKEN_VALID")


def test_r16_token_for_other_cart_denies(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    tok = make_stepup(w, intent, cart, cart_id="cm_other")
    d = decide(w, intent, proposal, cart, stepup=tok)
    assert (d.verdict, d.rule_id) == (DENY, "R16_STEPUP_TOKEN_VALID")


def test_r16_under_approved_token_denies(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    tok = make_stepup(w, intent, cart, approved_total_paise=cart.payload["total_paise"] - 1)
    d = decide(w, intent, proposal, cart, stepup=tok)
    assert (d.verdict, d.rule_id) == (DENY, "R16_STEPUP_TOKEN_VALID")


def test_r16_token_signed_by_wrong_key_denies(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    tok = make_stepup(w, intent, cart)
    forged = sign(tok.payload, w.keys.agent, "user")
    d = decide(w, intent, proposal, cart, stepup=forged)
    assert (d.verdict, d.rule_id) == (DENY, "R16_STEPUP_TOKEN_VALID")


def test_exactly_at_cap_allows(w):
    intent, proposal, cart = happy_chain(w, items=[ProposalItem("GHEE1", 2), ProposalItem("DAL1", 1), ProposalItem("OIL1", 1)])
    assert cart.payload["total_paise"] == 150_000
    assert decide(w, intent, proposal, cart).verdict == ALLOW


def test_decision_to_dict_is_json_shaped(w):
    d = decide(w, *happy_chain(w))
    as_dict = d.to_dict()
    assert as_dict["verdict"] == "ALLOW" and isinstance(as_dict["checks"], list)
    assert set(as_dict["checks"][0]) == {"rule_id", "passed", "detail"}


def test_r08_proposal_references_other_intent(w):
    intent_a, intent_b = make_intent(w), make_intent(w)
    proposal = make_proposal(w, intent_a)
    cart_b = resign_cart(w, w.merchant.quote(proposal), intent_id=intent_b.payload["intent_id"])
    d = decide(w, intent_b, proposal, cart_b)
    assert (d.verdict, d.rule_id) == (DENY, "R08_CART_CHAIN")
    assert "proposal references intent" in d.reason


def test_r08_proposal_addressed_to_other_merchant(w):
    intent = make_intent(w, merchant_allowlist=[MERCHANT_ID, "other-shop"])
    cart = w.merchant.quote(make_proposal(w, intent))
    bad = make_proposal(w, intent, merchant_id="other-shop")
    d = decide(w, intent, bad, resign_cart(w, cart, proposal_id=bad.payload["proposal_id"]))
    assert (d.verdict, d.rule_id) == (DENY, "R08_CART_CHAIN")
    assert "addressed to" in d.reason


def test_r10_rejects_negative_quantity_line(w):
    intent = make_intent(w)
    proposal = make_proposal(w, intent, items=[ProposalItem("GHEE1", 3), ProposalItem("RICE5", -1)])
    base = w.merchant.quote(make_proposal(w, intent, items=[ProposalItem("GHEE1", 3)]))
    items = list(base.payload["items"]) + [{"sku": "RICE5", "title": "Basmati Rice 5 kg", "category": "groceries", "qty": -1, "unit_price_paise": 45_000}]
    cart = resign_cart(w, base, items=items, total_paise=135_000, proposal_id=proposal.payload["proposal_id"])
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R10_CART_TOTAL_INTEGRITY")


def test_r10_rejects_empty_cart(w):
    intent = make_intent(w)
    proposal = sign(AgentProposal(new_id("ap"), AGENT_ID, intent.payload["intent_id"], MERCHANT_ID, [], "nothing", w.now).to_payload(), w.keys.agent, f"agent:{AGENT_ID}")
    base = w.merchant.quote(make_proposal(w, intent))
    cart = resign_cart(w, base, items=[], total_paise=0, proposal_id=proposal.payload["proposal_id"])
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (DENY, "R10_CART_TOTAL_INTEGRITY")


def test_r11_extra_unproposed_item_denies(w):
    intent, proposal, cart = happy_chain(w)
    items = list(cart.payload["items"]) + [{"sku": "MILK1", "title": "Toned Milk 1 L", "category": "groceries", "qty": 1, "unit_price_paise": 6_500}]
    d = decide(w, intent, proposal, resign_cart(w, cart, items=items, total_paise=cart.payload["total_paise"] + 6_500))
    assert (d.verdict, d.rule_id) == (DENY, "R11_CART_MATCHES_PROPOSAL")
    assert "MILK1" in d.reason


def test_r16_token_for_other_intent_denies(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    tok = make_stepup(w, intent, cart, intent_id="im_other")
    d = decide(w, intent, proposal, cart, stepup=tok)
    assert (d.verdict, d.rule_id) == (DENY, "R16_STEPUP_TOKEN_VALID")


def test_huge_total_fails_closed_without_raising(w):
    intent = make_intent(w)
    proposal = make_proposal(w, intent, items=[ProposalItem("RICE5", 1)])
    base = w.merchant.quote(proposal)
    huge = resign_cart(w, base, items=[{**base.payload["items"][0], "unit_price_paise": 10**400}], total_paise=10**400)
    d = decide(w, intent, proposal, huge)
    assert (d.verdict, d.rule_id) == (STEP_UP, "R14_PER_TXN_CAP")


def test_r99_internal_error_is_a_deny_with_trail(w):
    intent, proposal, cart = happy_chain(w)
    gi = make_gate_input(w, intent, proposal, cart)
    gi.merchant_pubs = None
    d = w.gate.evaluate(gi)
    assert (d.verdict, d.rule_id) == (DENY, "R99_GATE_ERROR")
    assert [c.rule_id for c in d.checks][:1] == ["R00_WELL_FORMED"] and d.checks[-1].rule_id == "R99_GATE_ERROR"


# --- Part II: delegation chains (R18, R19) ---


def check(d, rule_id):
    return next(c for c in d.checks if c.rule_id == rule_id)


def test_delegated_happy_path_allows(w):
    intent, sub, proposal, cart = delegated_chain(w)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert d.verdict == ALLOW
    assert check(d, "R18_DELEGATION_CHAIN").passed and "planner-01 -> shopper-01" in check(d, "R18_DELEGATION_CHAIN").detail
    assert check(d, "R19_DELEGATION_SUBSET").passed


def test_undelegated_trail_has_nineteen_checks(w):
    d = decide(w, *happy_chain(w))
    assert d.verdict == ALLOW and len(d.checks) == 19
    assert check(d, "R18_DELEGATION_CHAIN").passed and "no delegation" in check(d, "R18_DELEGATION_CHAIN").detail
    assert check(d, "R19_DELEGATION_SUBSET").passed


def test_r18_delegator_not_registered(w):
    intent, sub, proposal, cart = delegated_chain(w)
    r2 = AgentRegistry()
    r2.register(AGENT_ID, w.keys.pub("agent"))
    w.gate = PolicyGate(r2)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "not an active registered agent" in d.reason


def test_r18_delegator_revoked(w):
    intent, sub, proposal, cart = delegated_chain(w)
    w.registry.revoke(PLANNER_ID)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")


def test_r18_sub_signed_by_wrong_key(w):
    intent, sub, proposal, cart = delegated_chain(w, delegator_key=w.keys.merchant)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "does not verify" in d.reason


def test_r18_wrong_parent_id(w):
    intent, sub, proposal, cart = delegated_chain(w, parent_id="im_other")
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "is not the previous link" in d.reason


def test_r18_delegator_is_not_parents_agent(w):
    intent = make_intent(w)  # delegates straight to shopper-01, so the planner has nothing to narrow
    sub = make_sub(w, intent)  # ...but signs a sub-mandate anyway
    proposal = make_proposal(w, intent)
    d = decide(w, intent, proposal, w.merchant.quote(proposal), chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "is not the previous link's agent" in d.reason


def test_r19_total_cap_over_parent(w):
    intent, sub, proposal, cart = delegated_chain(w, max_total_paise=500_000)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")
    assert "5,000.00" in d.reason and "2,000.00" in d.reason


def test_r19_per_txn_over_parent(w):
    intent, sub, proposal, cart = delegated_chain(w, max_per_txn_paise=200_000)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")


def test_r19_merchant_not_in_parent(w):
    intent, sub, proposal, cart = delegated_chain(w, merchant_allowlist=[MERCHANT_ID, "other-shop"])
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")
    assert "other-shop" in d.reason


def test_r19_category_not_in_parent(w):
    intent, sub, proposal, cart = delegated_chain(w, categories=["groceries", "electronics"])
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")
    assert "electronics" in d.reason


def test_r19_expiry_later_than_parent(w):
    intent = make_intent(w, agent_id=PLANNER_ID)
    sub = make_sub(w, intent, expires_at=intent.payload["expires_at"] + 1)
    proposal = make_proposal(w, intent)
    d = decide(w, intent, proposal, w.merchant.quote(proposal), chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")
    assert "later than the parent" in d.reason


def test_r19_expired_sub(w):
    intent, sub, proposal, cart = delegated_chain(w, expires_at=w.now)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")
    assert "expired" in d.reason


def test_r06_proposal_from_planner_not_leaf(w):
    intent = make_intent(w, agent_id=PLANNER_ID)
    sub = make_sub(w, intent)
    payload = AgentProposal(new_id("ap"), PLANNER_ID, intent.payload["intent_id"], MERCHANT_ID, list(HAPPY_ITEMS), "planner buying directly", w.now).to_payload()
    proposal = sign(payload, w.keys.planner, f"agent:{PLANNER_ID}")
    d = decide(w, intent, proposal, w.merchant.quote(proposal), chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R06_INTENT_AGENT_MATCH")
    assert "'shopper-01'" in d.reason and "'planner-01'" in d.reason


def test_r14_sub_cap_requests_step_up_naming_the_link(w):
    intent, sub, proposal, cart = delegated_chain(w, max_per_txn_paise=50_000)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (STEP_UP, "R14_PER_TXN_CAP")
    assert "'shopper-01'" in d.reason and "910.00" in d.reason and "500.00" in d.reason


def test_r15_sub_total_cap_with_prior_spend(w):
    intent, sub, proposal, cart = delegated_chain(w, max_total_paise=100_000)
    d = decide(w, intent, proposal, cart, chain=[sub], spent_by={sub.payload["sub_id"]: 60_000})
    assert (d.verdict, d.rule_id) == (STEP_UP, "R15_TOTAL_CAP")
    assert "'shopper-01'" in d.reason and "600.00" in d.reason and "1,000.00" in d.reason


# --- Part II: refunds are money actions too (RF00-RF08) ---

RF_TRAIL = [
    "RF00_WELL_FORMED", "RF01_CART_SIG", "RF02_PAYMENT_SIG", "RF03_ATTESTATION_SIG",
    "RF04_ATTESTATION_NOT_EXPIRED", "RF05_PAYMENT_CAPTURED", "RF06_SHORTFALL_INTEGRITY",
    "RF07_REFUND_WITHIN_CAPTURE", "RF08_NO_DUPLICATE",
]


def refund_chain(w, lines=(("OIL1", 1),), **over):
    """intent -> proposal -> cart -> gate-signed payment mandate -> merchant-signed shortfall attestation."""
    _, _, cart = happy_chain(w)
    payment = make_payment_mandate(w, cart)
    return make_shortfall(w, cart, payment, lines=lines, **over), cart, payment


def refund_decide(w, att, cart, payment, **kw):
    return w.gate.evaluate_refund(make_refund_input(w, att, cart, payment, **kw))


def test_refund_happy_path_allows_with_full_trail(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, cart, payment)
    assert (d.verdict, d.rule_id) == (ALLOW, "ALLOW")
    assert [c.rule_id for c in d.checks] == RF_TRAIL
    assert all(c.passed for c in d.checks)
    assert "RF99_GATE_ERROR" not in [c.rule_id for c in d.checks]
    assert "140.00" in d.reason and payment.payload["payment_id"] in d.reason


def test_rf00_malformed_attestation_denies(w):
    att, cart, payment = refund_chain(w)
    malformed = sign({**att.payload, "note": "extra field"}, w.keys.merchant, att.signer)
    d = refund_decide(w, malformed, cart, payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF00_WELL_FORMED")
    assert "unknown keys" in d.reason


def test_rf01_cart_signed_by_wrong_key_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, sign(cart.payload, w.keys.user, cart.signer), payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF01_CART_SIG")


def test_rf02_payment_signed_by_wrong_key_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, cart, sign(payment.payload, w.keys.agent, payment.signer))
    assert (d.verdict, d.rule_id) == (DENY, "RF02_PAYMENT_SIG")
    assert "gate key" in d.reason


def test_rf02_payment_for_another_cart_denies(w):
    att, cart, _ = refund_chain(w)
    other_payment = make_payment_mandate(w, happy_chain(w)[2])
    d = refund_decide(w, att, cart, other_payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF02_PAYMENT_SIG")
    assert "not" in d.reason and cart.payload["cart_id"] in d.reason


def test_rf03_attestation_signed_by_wrong_key_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, sign(att.payload, w.keys.agent, att.signer), cart, payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF03_ATTESTATION_SIG")
    assert "does not verify" in d.reason


def test_rf03_attestation_for_another_payment_denies(w):
    att, cart, payment = refund_chain(w, payment_id="pm_somewhere_else")
    d = refund_decide(w, att, cart, payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF03_ATTESTATION_SIG")
    assert "pm_somewhere_else" in d.reason


def test_rf04_expired_attestation_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, cart, payment, now=att.payload["expires_at"])
    assert (d.verdict, d.rule_id) == (DENY, "RF04_ATTESTATION_NOT_EXPIRED")


def test_rf05_nothing_captured_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, cart, payment, captured_paise=0)
    assert (d.verdict, d.rule_id) == (DENY, "RF05_PAYMENT_CAPTURED")


def test_rf06_inflated_refund_amount_denies(w):
    att, cart, payment = refund_chain(w, refund_paise=90_000)
    d = refund_decide(w, att, cart, payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF06_SHORTFALL_INTEGRITY")
    assert "900.00" in d.reason and "140.00" in d.reason


def test_rf06_qty_short_above_the_cart_line_denies(w):
    att, cart, payment = refund_chain(w, lines=(("OIL1", 2),))
    d = refund_decide(w, att, cart, payment)
    assert (d.verdict, d.rule_id) == (DENY, "RF06_SHORTFALL_INTEGRITY")
    assert "'OIL1'" in d.reason


def test_rf07_refund_over_the_remaining_capture_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, cart, payment, captured_paise=91_000, refunded_paise=80_000)
    assert (d.verdict, d.rule_id) == (DENY, "RF07_REFUND_WITHIN_CAPTURE")
    assert "110.00" in d.reason


def test_rf08_duplicate_shortfall_denies(w):
    att, cart, payment = refund_chain(w)
    d = refund_decide(w, att, cart, payment, seen=[att.payload["shortfall_id"]])
    assert (d.verdict, d.rule_id) == (DENY, "RF08_NO_DUPLICATE")
    assert att.payload["shortfall_id"] in d.reason


def test_rf99_internal_error_is_a_deny_with_trail(w):
    att, cart, payment = refund_chain(w)
    ri = make_refund_input(w, att, cart, payment)
    ri.merchant_pubs = None
    d = w.gate.evaluate_refund(ri)
    assert (d.verdict, d.rule_id) == (DENY, "RF99_GATE_ERROR")
    assert [c.rule_id for c in d.checks] == ["RF00_WELL_FORMED", "RF99_GATE_ERROR"]


def test_r18_pass_is_recorded_even_when_r19_denies(w):
    intent, sub, proposal, cart = delegated_chain(w, max_total_paise=500_000, max_per_txn_paise=500_000)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert (d.verdict, d.rule_id) == (DENY, "R19_DELEGATION_SUBSET")
    r18 = [c for c in d.checks if c.rule_id == "R18_DELEGATION_CHAIN"]
    assert len(r18) == 1 and r18[0].passed


def test_r18_rejects_a_repeated_sub_id(w):
    intent = make_intent(w, agent_id=PLANNER_ID)
    first = make_sub(w, intent)
    second = make_sub(w, first, parent_id=first.payload["sub_id"], sub_id=first.payload["sub_id"], delegator_id=AGENT_ID, delegator_key=w.keys.agent)
    proposal = make_proposal(w, intent)
    cart = w.merchant.quote(proposal)
    d = decide(w, intent, proposal, cart, chain=[first, second])
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "repeats an earlier link" in d.reason


def test_r18_rejects_an_over_long_chain(w):
    intent = make_intent(w, agent_id=PLANNER_ID)
    chain, parent = [], intent
    for _ in range(9):
        s = make_sub(w, parent)
        chain.append(s)
        parent = s
    proposal = make_proposal(w, intent)
    cart = w.merchant.quote(proposal)
    d = decide(w, intent, proposal, cart, chain=chain)
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "exceeds the maximum" in d.reason


def test_r18_length_bounds_the_parse_so_a_padded_chain_is_cheap(w):
    intent = make_intent(w, agent_id=PLANNER_ID)
    sub = make_sub(w, intent)  # one signed link, repeated: only the first nine are ever parsed
    proposal = make_proposal(w, intent)
    cart = w.merchant.quote(proposal)
    started = time.perf_counter()
    d = decide(w, intent, proposal, cart, chain=[sub] * 500)
    elapsed = time.perf_counter() - started
    assert (d.verdict, d.rule_id) == (DENY, "R18_DELEGATION_CHAIN")
    assert "delegation chain of 500 links exceeds the maximum of 8" in d.reason
    assert elapsed < 0.5, f"rejecting a padded chain took {elapsed:.3f}s"


def test_delegated_details_name_index_and_agent(w):
    intent, sub, proposal, cart = delegated_chain(w, max_per_txn_paise=50_000)
    d = decide(w, intent, proposal, cart, chain=[sub])
    assert d.verdict == STEP_UP and "first breach root-first" in d.reason


def test_undelegated_cap_details_carry_no_delegation_vocabulary(w):
    intent, proposal, cart = happy_chain(w, items=STEPUP_ITEMS)
    d = decide(w, intent, proposal, cart)
    assert (d.verdict, d.rule_id) == (STEP_UP, "R14_PER_TXN_CAP")
    assert "first breach root-first" not in d.reason and "link '" not in d.reason
    assert d.reason.startswith("cart INR 1,800.00 exceeds the per-transaction cap INR 1,500.00")
    assert "tightest" not in check(decide(w, *happy_chain(w)), "R14_PER_TXN_CAP").detail
    d_intent, sub, d_proposal, d_cart = delegated_chain(w, max_per_txn_paise=50_000)
    delegated = decide(w, d_intent, d_proposal, d_cart, chain=[sub])
    assert delegated.verdict == STEP_UP
    assert "first breach root-first" in delegated.reason and "link '" in delegated.reason
