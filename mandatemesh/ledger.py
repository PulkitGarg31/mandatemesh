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


class Ledger:
    def __init__(self, path: Path, clock: Callable[[], int] | None = None) -> None:
        self.path = path
        self._clock = clock or (lambda: int(time.time()))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._events: list[Event] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    self._events.append(Event(**json.loads(line)))

    @property
    def head_hash(self) -> str:
        return self._events[-1].hash if self._events else GENESIS_HASH

    def append(self, type: str, actor: str, payload: dict) -> Event:
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
        for ev in self._events:
            unhashed = ev.to_dict()
            expected = unhashed.pop("hash")
            if ev.prev_hash != prev or compute_hash(prev, unhashed) != expected:
                return False, ev.seq
            prev = ev.hash
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
        ids = {payment_id, created.payload["intent_id"], created.payload["cart_id"]}
        related = [e for e in self._events if any(i in json.dumps(e.payload) for i in ids)]
        decisions = [e for e in related if e.type == "gate.decision"]
        captured = next((e for e in related if e.type == "payment.captured"), None)

        lines = [
            f"# Receipt for payment mandate `{payment_id}`",
            "",
            f"- Intent mandate: `{created.payload['intent_id']}`",
            f"- Cart mandate: `{created.payload['cart_id']}`",
            f"- Amount: INR {created.payload.get('amount_paise', 0) / 100:,.2f}",
            f"- Outcome: {'captured as ' + str(captured.payload.get('razorpay_payment_id')) if captured else 'not captured'}",
            f"- Ledger head hash: `{self.head_hash}`",
            "",
        ]
        if decisions:
            last = decisions[-1].payload
            lines += [f"## Gate decision: {last.get('verdict')} ({last.get('rule_id')})", "", last.get("reason", ""), "", "| rule | passed | detail |", "|---|---|---|"]
            lines += [f"| {c['rule_id']} | {'yes' if c['passed'] else 'NO'} | {c['detail']} |" for c in last.get("checks", [])]
            lines.append("")
        lines += ["## Events", "", "| seq | ts | type | actor | summary |", "|---|---|---|---|---|"]
        for e in related:
            summary = json.dumps({k: v for k, v in e.payload.items() if k not in ("envelope", "checks")}, ensure_ascii=True)
            lines.append(f"| {e.seq} | {e.ts} | {e.type} | {e.actor} | {summary[:120]} |")
        return "\n".join(lines) + "\n"


def tamper(path: Path, seq: int) -> None:
    """Demo helper: edit one event in place WITHOUT recomputing its hash, so verify() fails at seq."""
    lines = path.read_text(encoding="utf-8").splitlines()
    ev = json.loads(lines[seq])
    if "amount_paise" in ev["payload"]:
        ev["payload"]["amount_paise"] = int(ev["payload"]["amount_paise"]) * 10
    else:
        ev["payload"]["_tampered"] = True
    lines[seq] = json.dumps(ev, ensure_ascii=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
