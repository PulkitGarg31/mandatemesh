import json

import pytest

from mandatemesh.crypto import sign, verify
from mandatemesh.keys import Keys
from mandatemesh.mandates import AgentProposal, CartMandate, ProposalItem, ShortfallAttestation, ShortLine
from mandatemesh.merchant import DEFAULT_FEED, MerchantError, MockMerchant

NOW = 1_800_000_000
HAPPY = [ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]


@pytest.fixture
def world():
    keys = Keys.generate()
    merchant = MockMerchant("kirana-one", keys.merchant, clock=lambda: NOW)
    return keys, merchant


def proposal_env(keys, items, merchant_id="kirana-one"):
    p = AgentProposal("ap_1", "shopper-01", "im_1", merchant_id, items, "test", NOW)
    return sign(p.to_payload(), keys.agent, "agent:shopper-01")


def cart_env(keys, merchant, items=None):
    return merchant.quote(proposal_env(keys, list(items or HAPPY)))


def test_catalog_loads_ten_items(world):
    _, merchant = world
    items = merchant.catalog()
    assert len(items) == 10
    assert {"item_id", "title", "description", "url", "price_paise", "currency", "availability", "image_url", "category"} <= set(items[0])
    assert "SYSTEM OVERRIDE" in merchant.catalog_json()


def test_quote_price_locks_and_signs(world):
    keys, merchant = world
    env = merchant.quote(proposal_env(keys, [ProposalItem("RICE5", 1), ProposalItem("DAL1", 2), ProposalItem("OIL1", 1)]))
    assert verify(env, merchant.pubkey_b64)
    cart = CartMandate.from_payload(env.payload)
    assert cart.total_paise == 45_000 + 2 * 16_000 + 14_000
    assert cart.intent_id == "im_1" and cart.proposal_id == "ap_1"
    assert cart.items[0].category == "groceries"
    assert cart.expires_at == NOW + 600


def test_quote_rejects_unknown_sku(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="unknown sku"):
        merchant.quote(proposal_env(keys, [ProposalItem("NOPE", 1)]))


def test_quote_rejects_out_of_stock(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="out of stock"):
        merchant.quote(proposal_env(keys, [ProposalItem("SALT1", 1)]))


def test_quote_rejects_other_merchant_and_empty_cart(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="addressed to"):
        merchant.quote(proposal_env(keys, [ProposalItem("RICE5", 1)], merchant_id="other-shop"))
    with pytest.raises(MerchantError, match="empty"):
        merchant.quote(proposal_env(keys, []))


def test_quote_rejects_malformed_proposal_as_merchant_error(world):
    keys, merchant = world
    env = proposal_env(keys, [ProposalItem("RICE5", 1)])
    bad = sign({**env.payload, "items": [{"sku": "RICE5", "qty": 2.0}]}, keys.agent, env.signer)
    with pytest.raises(MerchantError, match="malformed proposal"):
        merchant.quote(bad)


@pytest.mark.parametrize("qty", [0, -1])
def test_quote_rejects_non_positive_quantity(world, qty):
    keys, merchant = world
    with pytest.raises(MerchantError, match="invalid qty"):
        merchant.quote(proposal_env(keys, [ProposalItem("RICE5", qty)]))


def test_attest_shortfall_prices_the_short_lines_and_signs(world):
    keys, merchant = world
    cart = cart_env(keys, merchant)
    env = merchant.attest_shortfall(cart, "pm_1", [ShortLine("OIL1", 1)])
    assert verify(env, merchant.pubkey_b64) and env.signer == "merchant:kirana-one"
    att = ShortfallAttestation.from_payload(env.payload)
    assert att.refund_paise == 14_000
    assert att.cart_id == cart.payload["cart_id"] and att.payment_id == "pm_1"
    assert att.lines == [ShortLine("OIL1", 1)]
    assert att.issued_at == NOW and att.expires_at == NOW + 600
    assert att.shortfall_id.startswith("sf_")


def test_attest_shortfall_rejects_sku_not_on_the_cart(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="not on cart"):
        merchant.attest_shortfall(cart_env(keys, merchant), "pm_1", [ShortLine("GHEE1", 1)])


def test_attest_shortfall_rejects_zero_quantity(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="invalid qty_short"):
        merchant.attest_shortfall(cart_env(keys, merchant), "pm_1", [ShortLine("OIL1", 0)])


def test_attest_shortfall_rejects_more_than_the_cart_line(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="invalid qty_short"):
        merchant.attest_shortfall(cart_env(keys, merchant), "pm_1", [ShortLine("DAL1", 3)])


def test_attest_shortfall_rejects_empty_lines(world):
    keys, merchant = world
    with pytest.raises(MerchantError, match="empty shortfall"):
        merchant.attest_shortfall(cart_env(keys, merchant), "pm_1", [])


def test_feed_with_non_integer_price_is_rejected_at_load(tmp_path):
    feed = json.loads(DEFAULT_FEED.read_text(encoding="utf-8"))
    feed["items"][0]["price_paise"] = 450.0
    bad = tmp_path / "feed.json"
    bad.write_text(json.dumps(feed), encoding="utf-8")
    with pytest.raises(MerchantError, match="price_paise"):
        MockMerchant("kirana-one", Keys.generate().merchant, feed_path=bad)
