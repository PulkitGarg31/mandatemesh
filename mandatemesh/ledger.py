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


# --- Replay. The gate is a pure function and every input it consumed is in the ledger, so anyone
# holding the file can re-decide each decision and compare, without trusting the process that wrote it. ---

_DECISION_FIELDS = ("verdict", "rule_id", "reason", "checks")


class _Unreplayable(Exception):
    """An input the decision consumed is not in the ledger, so that decision cannot be re-decided."""


@dataclass
class ReplayRow:
    seq: int
    kind: str          # "purchase" | "refund"
    recorded: str      # recorded verdict
    replayed: str      # recomputed verdict
    identical: bool
    note: str = ""     # why not identical, or why it could not be replayed


@dataclass
class ReplayReport:
    rows: list[ReplayRow]

    @property
    def decisions(self) -> int:
        return len(self.rows)

    @property
    def identical(self) -> int:
        return sum(1 for r in self.rows if r.identical)

    @property
    def first_divergence(self) -> ReplayRow | None:
        return next((r for r in self.rows if not r.identical), None)


def _explain(diffs: list[str], recorded: dict, got: dict) -> str:
    if not diffs:
        return ""
    bits = []
    for f in diffs:
        if f == "checks":
            bits.append(f"checks: {len(recorded[f] or [])} recorded, {len(got[f])} replayed")
        else:
            bits.append(f"{f}: recorded {recorded[f]!r}, replayed {got[f]!r}"[:300])
    return "the recorded decision is not what the gate recomputes -- " + "; ".join(bits)


def _pubkey(ev: Event, key: str) -> str:
    pub = ev.payload.get(key)
    if not isinstance(pub, str):
        raise _Unreplayable(f"the {ev.type} event at seq {ev.seq} carries no {key}")
    return pub


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

    def spent_for(self, mandate_id: str) -> int:
        """Net money moved under one mandate link: captures under it, minus refunds against those payments.

        A capture counts for its root intent and for every id in its `chain_ids`, so one delegated purchase
        is charged against the root mandate and against each sub-mandate it was made under.
        """
        captured = [
            e for e in self.of_type("payment.captured")
            if e.payload.get("intent_id") == mandate_id or mandate_id in (e.payload.get("chain_ids") or [])
        ]
        payment_ids = {e.payload.get("payment_id") for e in captured} - {None}
        refunded = sum(
            int(e.payload.get("amount_paise", 0))
            for e in self.of_type("refund.created")
            if e.payload.get("payment_id") in payment_ids
        )
        return max(0, sum(int(e.payload.get("amount_paise", 0)) for e in captured) - refunded)

    # --- Offline replay: re-decide every gate decision from the events that precede it ---

    def replay(self) -> ReplayReport:
        """Re-run the gate on every decision in this ledger and compare. Never raises; a broken input is a row."""
        # Lazy so that gate.py and ledger.py stay independent at import time: the gate must not know about the ledger.
        from mandatemesh import crypto, gate, registry

        rows: list[ReplayRow] = []
        for k, ev in enumerate(self._events):
            kind = {"gate.decision": "purchase", "refund.decision": "refund"}.get(ev.type)
            if kind is None:
                continue
            recorded = {f: ev.payload.get(f) for f in _DECISION_FIELDS}
            try:
                policy = gate.PolicyGate(self._registry_before(k, registry))
                if kind == "purchase":
                    d = policy.evaluate(self._gate_input(k, ev, gate, crypto))
                else:
                    d = policy.evaluate_refund(self._refund_input(k, ev, gate, crypto))
            except _Unreplayable as exc:
                rows.append(ReplayRow(k, kind, str(recorded["verdict"]), "", False, str(exc)))
                continue
            except Exception as exc:  # replay is an audit tool: a surprise is a finding, never a crash
                note = f"could not replay this decision: {type(exc).__name__}: {str(exc)[:200]}"
                rows.append(ReplayRow(k, kind, str(recorded["verdict"]), "", False, note))
                continue
            got = d.to_dict()
            diffs = [f for f in _DECISION_FIELDS if got[f] != recorded[f]]
            rows.append(ReplayRow(k, kind, str(recorded["verdict"]), d.verdict, not diffs, _explain(diffs, recorded, got)))
        return ReplayReport(rows)

    def _registry_before(self, k: int, registry):
        """Registry state as of seq k, rebuilt from the registration events alone."""
        reg = registry.AgentRegistry()
        for e in self._events[:k]:
            agent_id = e.payload.get("agent_id")
            if e.type == "agent.registered":
                rec = reg.get(agent_id)
                if rec is not None and rec.status == registry.REVOKED:
                    continue  # the live registry refuses to re-register a revoked agent, so neither do we
                reg.register(agent_id, e.payload.get("pubkey", ""))
            elif e.type == "agent.revoked" and reg.get(agent_id) is not None:
                reg.revoke(agent_id)
        return reg

    def _envelope_before(self, k: int, type: str, key: str, value: object, what: str) -> tuple[Event, dict]:
        for e in self._events[:k]:
            if e.type == type and e.payload.get(key) == value:
                env = e.payload.get("envelope")
                if not isinstance(env, dict):
                    raise _Unreplayable(f"the {type} event for {what} at seq {e.seq} carries no envelope")
                return e, env
        raise _Unreplayable(f"no {type} event for {what} before seq {k}")

    def _gate_input(self, k: int, ev: Event, gate, crypto):
        p = ev.payload
        intent_ev, intent = self._envelope_before(k, "mandate.intent.created", "intent_id", p.get("intent_id"), f"intent {p.get('intent_id')!r}")
        cart_ev, cart = self._envelope_before(k, "merchant.cart.quoted", "cart_id", p.get("cart_id"), f"cart {p.get('cart_id')!r}")
        cart_payload = cart.get("payload") or {}
        proposal_id = cart_payload.get("proposal_id")
        _, proposal = self._envelope_before(k, "agent.proposal", "proposal_id", proposal_id, f"proposal {proposal_id!r} behind cart {p.get('cart_id')!r}")
        chain = []
        for sub_id in list(p.get("chain_ids") or [])[1:]:  # chain_ids[0] is the root intent, not a sub-mandate
            _, sub = self._envelope_before(k, "mandate.sub.created", "sub_id", sub_id, f"sub-mandate {sub_id!r}")
            chain.append(crypto.Envelope.from_dict(sub))
        stepup = None
        if p.get("stepup_id") is not None:
            _, su = self._envelope_before(k, "stepup.approved", "stepup_id", p["stepup_id"], f"step-up token {p['stepup_id']!r}")
            stepup = crypto.Envelope.from_dict(su)
        return gate.GateInput(
            intent=crypto.Envelope.from_dict(intent),
            proposal=crypto.Envelope.from_dict(proposal),
            cart=crypto.Envelope.from_dict(cart),
            user_pub_b64=_pubkey(intent_ev, "user_pubkey"),
            merchant_pubs={cart_payload.get("merchant_id"): _pubkey(cart_ev, "merchant_pubkey")},
            spent_paise=int(p.get("spent_paise", 0)),
            now=int(p.get("now", 0)),
            stepup=stepup,
            chain=chain,
            spent_by={str(sub_id): int(spent) for sub_id, spent in (p.get("spent_by") or {}).items()},
        )

    def _refund_input(self, k: int, ev: Event, gate, crypto):
        p = ev.payload
        _, att = self._envelope_before(k, "merchant.shortfall", "shortfall_id", p.get("shortfall_id"), f"shortfall {p.get('shortfall_id')!r}")
        cart_ev, cart = self._envelope_before(k, "merchant.cart.quoted", "cart_id", p.get("cart_id"), f"cart {p.get('cart_id')!r}")
        _, pay = self._envelope_before(k, "mandate.payment.created", "payment_id", p.get("payment_id"), f"payment mandate {p.get('payment_id')!r}")
        return gate.RefundInput(
            attestation=crypto.Envelope.from_dict(att),
            cart=crypto.Envelope.from_dict(cart),
            payment=crypto.Envelope.from_dict(pay),
            merchant_pubs={(cart.get("payload") or {}).get("merchant_id"): _pubkey(cart_ev, "merchant_pubkey")},
            gate_pub_b64=_pubkey(ev, "gate_pubkey"),
            captured_paise=int(p.get("captured_paise", 0)),
            refunded_paise=int(p.get("refunded_paise", 0)),
            seen_shortfalls=list(p.get("seen_shortfalls") or []),
            now=int(p.get("now", 0)),
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
