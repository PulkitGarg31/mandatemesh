import pytest

from mandatemesh.crypto import Envelope, sign
from mandatemesh.fixtures import (
    AGENT_ID, HAPPY_ITEMS, MERCHANT_ID, STEPUP_ITEMS, happy_chain, make_gate_input, make_intent, make_proposal,
    make_stepup, make_world, resign_cart,
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
