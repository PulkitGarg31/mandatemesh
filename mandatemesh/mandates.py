"""The mandate chain as plain data. Signing lives in crypto.py; rules live in gate.py."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class ProposalItem:
    sku: str
    qty: int


@dataclass
class CartItem:
    sku: str
    title: str
    category: str
    qty: int
    unit_price_paise: int


@dataclass
class IntentMandate:
    """Signed by the user. Delegates bounded spending authority to one agent."""

    intent_id: str
    user_id: str
    agent_id: str
    currency: str
    max_total_paise: int
    max_per_txn_paise: int
    merchant_allowlist: list[str]
    categories: list[str]
    issued_at: int
    expires_at: int
    nonce: str

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "IntentMandate":
        return cls(**p)


@dataclass
class AgentProposal:
    """Signed by the agent. What the (untrusted) agent wants to buy."""

    proposal_id: str
    agent_id: str
    intent_id: str
    merchant_id: str
    items: list[ProposalItem]
    justification: str
    issued_at: int

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "AgentProposal":
        data = dict(p)
        data["items"] = [ProposalItem(**i) for i in p["items"]]
        return cls(**data)


@dataclass
class CartMandate:
    """Signed by the merchant. Price-locks exact SKUs and total for a short window."""

    cart_id: str
    intent_id: str
    proposal_id: str
    merchant_id: str
    items: list[CartItem]
    total_paise: int
    currency: str
    issued_at: int
    expires_at: int

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "CartMandate":
        data = dict(p)
        data["items"] = [CartItem(**i) for i in p["items"]]
        return cls(**data)


@dataclass
class StepUpToken:
    """Signed by the user. Human approval bound to one cart."""

    stepup_id: str
    intent_id: str
    cart_id: str
    approved_total_paise: int
    issued_at: int
    expires_at: int

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "StepUpToken":
        return cls(**p)


@dataclass
class PaymentMandate:
    """Signed by the gate. The only thing the executor will act on."""

    payment_id: str
    intent_id: str
    cart_id: str
    amount_paise: int
    currency: str
    issued_at: int

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "PaymentMandate":
        return cls(**p)
