"""Append-only, hash-chained JSONL audit ledger. Any edit to any line breaks verify()."""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from mandatemesh.crypto import canonical_json

GENESIS_HASH = "0" * 64


@dataclass
class Event:
    seq: int
    id: str
    ts: int
    type: str
    actor: str
    payload: dict
    prev_hash: str
    hash: str

    def to_dict(self) -> dict:
        return asdict(self)


def compute_hash(prev_hash: str, unhashed: dict) -> str:
    return hashlib.sha256((prev_hash + canonical_json(unhashed).decode("utf-8")).encode("utf-8")).hexdigest()


def _inr(paise: int) -> str:
    sign, mag = ("-" if paise < 0 else ""), abs(int(paise))
    return f"INR {sign}{mag // 100:,}.{mag % 100:02d}"


def _cell(text: object) -> str:
    """Make a value safe inside a Markdown table cell."""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


class Ledger:
    def __init__(self, path: Path, clock: Callable[[], int] | None = None) -> None:
        self.path = path
        self._clock = clock or (lambda: int(time.time()))
        self._events: list[Event] = []
        self._corrupt_seq: int | None = None
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    self._events.append(Event(**json.loads(line)))
                except (ValueError, TypeError):  # truncated or hand-edited line: stop here, verify() reports it
                    self._corrupt_seq = len(self._events)
                    break

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS_HASH

    def append(self, type: str, actor: str, payload: dict) -> Event:
        payload = json.loads(json.dumps(payload))  # what we hash is exactly what a reload produces (str keys, lists, no aliasing)
        unhashed = {
            "seq": len(self._events),
            "id": f"evt_{uuid.uuid4().hex[:12]}",
            "ts": self._clock(),
            "type": type,
            "actor": actor,
            "payload": payload,
            "prev_hash": self.head_hash,
        }
        ev = Event(**unhashed, hash=compute_hash(self.head_hash, unhashed))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=True) + "\n")
        self._events.append(ev)
        return ev

    def events(self) -> list[Event]:
        return list(self._events)

    def of_type(self, type: str) -> list[Event]:
        return [e for e in self._events if e.type == type]

    def verify(self) -> tuple[bool, int | None]:
        prev = GENESIS_HASH
        for i, ev in enumerate(self._events):
            unhashed = ev.to_dict()
            expected = unhashed.pop("hash")
            if ev.seq != i or ev.prev_hash != prev or compute_hash(prev, unhashed) != expected:
                return False, i
            prev = ev.hash
        if self._corrupt_seq is not None:
            return False, self._corrupt_seq
        return True, None

    def spent_for(self, intent_id: str) -> int:
        return sum(
            int(e.payload.get("amount_paise", 0))
            for e in self.of_type("payment.captured")
            if e.payload.get("intent_id") == intent_id
        )

    def receipt(self, payment_id: str) -> str:
        created = next((e for e in self.of_type("mandate.payment.created") if e.payload.get("payment_id") == payment_id), None)
        if created is None:
            raise KeyError(f"no payment mandate {payment_id} in ledger")
        intent_id, cart_id = created.payload["intent_id"], created.payload["cart_id"]

        def is_related(p: dict) -> bool:
            if p.get("payment_id") == payment_id or p.get("cart_id") == cart_id:
                return True
            return p.get("intent_id") == intent_id and "cart_id" not in p and "payment_id" not in p

        related = [e for e in self._events if is_related(e.payload)]
        decisions = [e for e in related if e.type == "gate.decision"]
        captured = next((e for e in self.of_type("payment.captured") if e.payload.get("payment_id") == payment_id), None)
        quoted = next((e for e in self.of_type("merchant.cart.quoted") if e.payload.get("cart_id") == cart_id), None)
        link = next((e for e in self.of_type("razorpay.link.created") if e.payload.get("payment_id") == payment_id), None)
        attempts = [e for e in related if e.type in ("payment.failed", "payment.timeout", "payment.captured")]
        ok, bad = self.verify()
        if captured is None:
            outcome = "not captured"
        elif captured.payload.get("razorpay_payment_id") is None:
            outcome = "captured (payment id not reported by the link)"
        else:
            outcome = f"captured as {captured.payload['razorpay_payment_id']}"

        lines = [
            f"# Receipt for payment mandate `{payment_id}`",
            "",
            f"- Intent mandate: `{intent_id}`",
            f"- Cart mandate: `{cart_id}`",
            f"- Amount: {_inr(created.payload.get('amount_paise', 0))}",
            f"- Payment link: `{link.payload.get('link_id')}` ({link.payload.get('short_url')})" if link else "- Payment link: none created",
            f"- Payment attempts: {len(attempts)}",
            f"- Outcome: {outcome}",
            f"- Ledger head hash: `{self.head_hash}`",
            f"- Chain: {'verified' if ok else f'BROKEN at seq {bad}'}",
            "",
        ]
        if quoted:
            cart = quoted.payload.get("envelope", {}).get("payload", {})
            lines += [f"## Cart from `{cart.get('merchant_id')}`", "", "| sku | title | qty | unit | line |", "|---|---|---|---|---|"]
            for it in cart.get("items", []):
                qty, unit = int(it.get("qty", 0)), int(it.get("unit_price_paise", 0))
                lines.append(f"| {_cell(it.get('sku'))} | {_cell(it.get('title'))} | {qty} | {_inr(unit)} | {_inr(qty * unit)} |")
            lines += [f"| | **total** | | | **{_inr(cart.get('total_paise', 0))}** |", ""]
        if decisions:
            last = decisions[-1].payload
            lines += [f"## Gate decision: {last.get('verdict')} ({last.get('rule_id')})", "", _cell(last.get("reason", "")), "", "| rule | passed | detail |", "|---|---|---|"]
            lines += [f"| {c['rule_id']} | {'yes' if c['passed'] else 'NO'} | {_cell(c['detail'])} |" for c in last.get("checks", [])]
            lines.append("")
        lines += ["## Events", "", "| seq | ts | type | actor | summary |", "|---|---|---|---|---|"]
        hidden = ("envelope", "checks", "intent_id", "cart_id", "payment_id")
        for e in related:
            summary = json.dumps({k: v for k, v in e.payload.items() if k not in hidden}, ensure_ascii=True)
            lines.append(f"| {e.seq} | {e.ts} | {e.type} | {_cell(e.actor)} | {_cell(summary[:120])} |")
        return "\n".join(lines) + "\n"


def tamper(path: Path, seq: int) -> None:
    """Demo helper: edit one event in place WITHOUT recomputing its hash, so verify() fails at seq."""
    lines = path.read_text(encoding="utf-8").splitlines()
    event_lines = [i for i, line in enumerate(lines) if line.strip()]
    if seq < 0 or seq >= len(event_lines):
        raise ValueError(f"no event at seq {seq}; ledger has {len(event_lines)} events")
    idx = event_lines[seq]
    ev = json.loads(lines[idx])
    if "amount_paise" in ev["payload"]:
        ev["payload"]["amount_paise"] = int(ev["payload"]["amount_paise"]) * 10
    else:
        ev["payload"]["_tampered"] = True
    lines[idx] = json.dumps(ev, ensure_ascii=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
