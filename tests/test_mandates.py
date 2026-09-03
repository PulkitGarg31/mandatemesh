import json

import pytest

from mandatemesh.crypto import canonical_json
from mandatemesh.mandates import (
    AgentProposal,
    CartItem,
    CartMandate,
    IntentMandate,
    MalformedMandate,
    PaymentMandate,
    ProposalItem,
    StepUpToken,
    SubMandate,
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


def test_sub_mandate_round_trip_and_strict_shape():
    s = SubMandate("sm_1", "im_1", "planner-01", "shopper-01", "INR", 100_000, 100_000, ["kirana-one"], ["groceries"], 100, 200, "n_1")
    assert SubMandate.from_payload(s.to_payload()) == s
    assert s.to_payload()["parent_id"] == "im_1"
    with pytest.raises(MalformedMandate, match=r"SubMandate: unknown keys \['extra'\]"):
        SubMandate.from_payload({**s.to_payload(), "extra": 1})


def test_from_payload_rejects_unknown_and_missing_keys():
    good = PaymentMandate("pm_1", "im_1", "cm_1", 1, "INR", 1).to_payload()
    with pytest.raises(MalformedMandate, match=r"unknown keys \['extra'\]"):
        PaymentMandate.from_payload({**good, "extra": 1})
    with pytest.raises(MalformedMandate, match=r"missing keys \['currency'\]"):
        PaymentMandate.from_payload({k: v for k, v in good.items() if k != "currency"})
    with pytest.raises(MalformedMandate, match="must be an object"):
        PaymentMandate.from_payload(["not", "a", "dict"])


def test_from_payload_rejects_wrong_scalar_types():
    good = PaymentMandate("pm_1", "im_1", "cm_1", 1, "INR", 1).to_payload()
    for bad in ("100", 100.0, True, None):
        with pytest.raises(MalformedMandate, match="amount_paise must be int"):
            PaymentMandate.from_payload({**good, "amount_paise": bad})
    intent = IntentMandate("im_1", "u", "a", "INR", 1, 1, ["m"], ["g"], 1, 2, "n").to_payload()
    with pytest.raises(MalformedMandate, match="merchant_allowlist must be a list"):
        IntentMandate.from_payload({**intent, "merchant_allowlist": "kirana-one"})
    with pytest.raises(MalformedMandate, match=r"categories\[0\] must be str"):
        IntentMandate.from_payload({**intent, "categories": [7]})


def test_from_payload_rejects_bad_nested_items():
    cart = CartMandate("cm_1", "im_1", "ap_1", "kirana-one", [CartItem("RICE5", "Rice", "groceries", 1, 45_000)], 45_000, "INR", 100, 700).to_payload()
    cart["items"][0]["bogus"] = 1
    with pytest.raises(MalformedMandate, match=r"items\[0\]: unknown keys \['bogus'\]"):
        CartMandate.from_payload(cart)
    proposal = AgentProposal("ap_1", "a", "im_1", "m", [ProposalItem("RICE5", 1)], "j", 1).to_payload()
    proposal["items"][0]["qty"] = "1"
    with pytest.raises(MalformedMandate, match=r"items\[0\]\.qty must be int"):
        AgentProposal.from_payload(proposal)


def test_wrong_mandate_type_is_malformed():
    with pytest.raises(MalformedMandate):
        IntentMandate.from_payload(StepUpToken("su_1", "im_1", "cm_1", 1, 1, 2).to_payload())


def test_json_text_round_trip_with_unicode_title():
    c = CartMandate(
        "cm_1", "im_1", "ap_1", "kirana-one",
        [CartItem("GHEE1", "गाय का घी 1 kg — ₹600", "groceries", 1, 60_000), CartItem("DAL1", "Toor Dal", "groceries", 2, 16_000)],
        92_000, "INR", 100, 700,
    )
    wire = json.loads(canonical_json(c.to_payload()))
    assert CartMandate.from_payload(wire) == c
