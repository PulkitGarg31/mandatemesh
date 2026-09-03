"""Mock merchant: an ACP-style feed plus signed, price-locked Cart Mandates. No LLM, no Razorpay."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mandatemesh.crypto import Envelope, public_b64, sign
from mandatemesh.mandates import (
    AgentProposal,
    CartItem,
    CartMandate,
    MalformedMandate,
    ShortfallAttestation,
    ShortLine,
    new_id,
)

CART_TTL_S = 600
DEFAULT_FEED = Path(__file__).resolve().parent.parent / "merchant_data" / "feed.json"


class MerchantError(Exception):
    """The merchant refused to quote or to attest (malformed mandate, unknown SKU, out of stock, wrong merchant, empty cart, bad quantity)."""


class MockMerchant:
    def __init__(
        self,
        merchant_id: str,
        private_key: Ed25519PrivateKey,
        feed_path: Path = DEFAULT_FEED,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.merchant_id = merchant_id
        self._key = private_key
        self._clock = clock or (lambda: int(time.time()))
        feed = json.loads(feed_path.read_text(encoding="utf-8"))
        for item in feed["items"]:
            for key in ("item_id", "title", "category", "availability", "price_paise"):
                if key not in item:
                    raise MerchantError(f"feed item {item.get('item_id', '?')!r} is missing {key!r}")
            if type(item["price_paise"]) is not int or item["price_paise"] < 0:
                raise MerchantError(f"feed item {item['item_id']!r} has a non-integer or negative price_paise")
        self._items: dict[str, dict] = {item["item_id"]: item for item in feed["items"]}

    @property
    def pubkey_b64(self) -> str:
        return public_b64(self._key)

    def catalog(self) -> list[dict]:
        return list(self._items.values())

    def catalog_json(self) -> str:
        return json.dumps(self.catalog(), ensure_ascii=False)

    def quote(self, proposal_env: Envelope) -> Envelope:
        try:
            proposal = AgentProposal.from_payload(proposal_env.payload)
        except MalformedMandate as exc:
            raise MerchantError(f"malformed proposal: {exc}") from exc
        if proposal.merchant_id != self.merchant_id:
            raise MerchantError(f"proposal addressed to '{proposal.merchant_id}', not '{self.merchant_id}'")
        if not proposal.items:
            raise MerchantError("empty cart")
        lines: list[CartItem] = []
        for it in proposal.items:
            item = self._items.get(it.sku)
            if item is None:
                raise MerchantError(f"unknown sku {it.sku}")
            if item["availability"] != "in_stock":
                raise MerchantError(f"{it.sku} is out of stock")
            if it.qty < 1:
                raise MerchantError(f"invalid qty {it.qty} for {it.sku}")
            lines.append(CartItem(sku=it.sku, title=item["title"], category=item["category"], qty=it.qty, unit_price_paise=item["price_paise"]))
        now = self._clock()
        cart = CartMandate(
            cart_id=new_id("cm"),
            intent_id=proposal.intent_id,
            proposal_id=proposal.proposal_id,
            merchant_id=self.merchant_id,
            items=lines,
            total_paise=sum(l.qty * l.unit_price_paise for l in lines),
            currency="INR",
            issued_at=now,
            expires_at=now + CART_TTL_S,
        )
        return sign(cart.to_payload(), self._key, f"merchant:{self.merchant_id}")

    def attest_shortfall(self, cart_env: Envelope, payment_id: str, lines: list[ShortLine]) -> Envelope:
        """Admit that paid-for lines could not be delivered. The merchant states the facts; the gate decides the refund."""
        try:
            cart = CartMandate.from_payload(cart_env.payload)
        except MalformedMandate as exc:
            raise MerchantError(f"malformed cart: {exc}") from exc
        if not lines:
            raise MerchantError("empty shortfall")
        by_sku = {line.sku: line for line in cart.items}
        refund_paise = 0
        for short in lines:
            item = by_sku.get(short.sku)
            if item is None:
                raise MerchantError(f"sku {short.sku} is not on cart {cart.cart_id}")
            if not 1 <= short.qty_short <= item.qty:
                raise MerchantError(f"invalid qty_short {short.qty_short} for {short.sku}; cart line is {item.qty}")
            refund_paise += short.qty_short * item.unit_price_paise
        now = self._clock()
        att = ShortfallAttestation(
            shortfall_id=new_id("sf"),
            cart_id=cart.cart_id,
            payment_id=payment_id,
            lines=list(lines),
            refund_paise=refund_paise,
            issued_at=now,
            expires_at=now + CART_TTL_S,
        )
        return sign(att.to_payload(), self._key, f"merchant:{self.merchant_id}")
