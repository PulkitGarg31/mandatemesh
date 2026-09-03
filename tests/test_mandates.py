from mandatemesh.mandates import (
    AgentProposal,
    CartItem,
    CartMandate,
    IntentMandate,
    PaymentMandate,
    ProposalItem,
    StepUpToken,
    new_id,
)


def test_new_id_has_prefix_and_is_unique():
    a, b = new_id("im"), new_id("im")
    assert a.startswith("im_") and len(a) == 15
    assert a != b


def test_intent_round_trip():
    m = IntentMandate("im_1", "user-01", "shopper-01", "INR", 200_000, 150_000, ["kirana-one"], ["groceries"], 100, 200, "n_1")
    assert IntentMandate.from_payload(m.to_payload()) == m
    assert m.to_payload()["merchant_allowlist"] == ["kirana-one"]


def test_proposal_round_trip_rebuilds_items():
    p = AgentProposal("ap_1", "shopper-01", "im_1", "kirana-one", [ProposalItem("RICE5", 1)], "staples", 100)
    back = AgentProposal.from_payload(p.to_payload())
    assert back == p
    assert isinstance(back.items[0], ProposalItem)


def test_cart_round_trip_rebuilds_items():
    c = CartMandate("cm_1", "im_1", "ap_1", "kirana-one", [CartItem("RICE5", "Rice", "groceries", 1, 45_000)], 45_000, "INR", 100, 700)
    back = CartMandate.from_payload(c.to_payload())
    assert back == c
    assert isinstance(back.items[0], CartItem)


def test_stepup_and_payment_round_trip():
    s = StepUpToken("su_1", "im_1", "cm_1", 180_000, 100, 700)
    p = PaymentMandate("pm_1", "im_1", "cm_1", 180_000, "INR", 100)
    assert StepUpToken.from_payload(s.to_payload()) == s
    assert PaymentMandate.from_payload(p.to_payload()) == p
