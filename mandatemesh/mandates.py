"""The mandate chain as plain data. Parsing is strict (shape and scalar types); signing lives in crypto.py; business rules live in gate.py."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, fields, is_dataclass
from typing import get_args, get_origin, get_type_hints


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class MalformedMandate(ValueError):
    """A payload does not have the exact shape (keys and scalar types) a mandate class requires."""


def _check(value, expected, where: str):
    if expected is int:
        if type(value) is not int:  # bool is deliberately not accepted as int
            raise MalformedMandate(f"{where} must be int, got {type(value).__name__}")
        return value
    if expected is str:
        if not isinstance(value, str):
            raise MalformedMandate(f"{where} must be str, got {type(value).__name__}")
        return value
    if get_origin(expected) is list:
        (inner,) = get_args(expected)
        if not isinstance(value, list):
            raise MalformedMandate(f"{where} must be a list, got {type(value).__name__}")
        return [_check(v, inner, f"{where}[{i}]") for i, v in enumerate(value)]
    if is_dataclass(expected):
        return _parse(expected, value, where)
    raise MalformedMandate(f"{where}: unsupported field type {expected!r}")


def _parse(cls, p, where: str):
    """Build `cls` from a dict, rejecting unknown keys, missing keys and wrong scalar types."""
    if not isinstance(p, dict):
        raise MalformedMandate(f"{where} must be an object, got {type(p).__name__}")
    hints = get_type_hints(cls)
    expected_keys = {f.name for f in fields(cls)}
    unknown = sorted(set(p) - expected_keys)
    missing = sorted(expected_keys - set(p))
    if unknown or missing:
        raise MalformedMandate(f"{where}: unknown keys {unknown}, missing keys {missing}")
    return cls(**{f.name: _check(p[f.name], hints[f.name], f"{where}.{f.name}") for f in fields(cls)})


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
        return _parse(cls, p, cls.__name__)


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
        return _parse(cls, p, cls.__name__)


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
        return _parse(cls, p, cls.__name__)


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
        return _parse(cls, p, cls.__name__)


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
        return _parse(cls, p, cls.__name__)


@dataclass
class ShortLine:
    sku: str
    qty_short: int


@dataclass
class ShortfallAttestation:
    """Signed by the merchant. Says which paid-for lines could not be delivered."""

    shortfall_id: str
    cart_id: str
    payment_id: str
    lines: list[ShortLine]
    refund_paise: int
    issued_at: int
    expires_at: int

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "ShortfallAttestation":
        return _parse(cls, p, cls.__name__)


@dataclass
class RefundMandate:
    """Signed by the gate. The only thing the executor will refund against."""

    refund_id: str
    payment_id: str
    razorpay_payment_id: str
    amount_paise: int
    currency: str
    issued_at: int

    def to_payload(self) -> dict:
        return asdict(self)

    @classmethod
    def from_payload(cls, p: dict) -> "RefundMandate":
        return _parse(cls, p, cls.__name__)


@dataclass
class SubMandate:
    """Signed by the delegator agent. Narrows a parent mandate for one delegate agent."""

    sub_id: str
    parent_id: str
    delegator_id: str
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
    def from_payload(cls, p: dict) -> "SubMandate":
        return _parse(cls, p, cls.__name__)
