"""Executors turn a signed PaymentMandate into a money action. This is the ONLY module that imports razorpay."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from mandatemesh.mandates import PaymentMandate

LINK_TTL_S = 20 * 60  # Razorpay requires expire_by >= 15 min in the future
PAID_STATUSES = {"captured", "authorized", "paid"}


@dataclass
class LinkInfo:
    link_id: str
    short_url: str
    status: str


@dataclass
class Attempt:
    payment_id: str
    status: str
    amount_paise: int


@dataclass
class PollResult:
    outcome: str  # "paid" | "failed" | "timeout"
    payment_id: str | None = None
    amount_paise: int = 0
    attempts: list[Attempt] = field(default_factory=list)


class Executor(Protocol):
    def create_payment_link(self, pm: PaymentMandate, description: str, notes: dict) -> LinkInfo: ...
    def poll(self, link_id: str, timeout_s: int, interval_s: float, seen: set[str]) -> PollResult: ...
    def cancel(self, link_id: str) -> None: ...


class FakeExecutor:
    """Scripted outcomes, no network. outcomes=['failed', 'paid'] -> first poll fails, second pays. [] -> timeouts."""

    def __init__(self, outcomes: list[str] | None = None) -> None:
        self.outcomes = ["paid"] if outcomes is None else list(outcomes)
        self.links: list[LinkInfo] = []
        self.amounts: dict[str, int] = {}
        self.cancelled: list[str] = []
        self._n = 0

    def create_payment_link(self, pm: PaymentMandate, description: str, notes: dict) -> LinkInfo:
        self._n += 1
        link = LinkInfo(f"plink_fake{self._n:03d}", f"https://rzp.io/fake/{self._n:03d}", "created")
        self.links.append(link)
        self.amounts[link.link_id] = pm.amount_paise
        return link

    def poll(self, link_id: str, timeout_s: int, interval_s: float, seen: set[str]) -> PollResult:
        outcome = self.outcomes.pop(0) if self.outcomes else "timeout"
        if outcome == "timeout":
            return PollResult("timeout")
        pid = f"pay_fake{len(seen) + 1:03d}"
        seen.add(pid)
        amount = self.amounts[link_id]
        status = "captured" if outcome == "paid" else "failed"
        return PollResult(outcome, pid, amount, [Attempt(pid, status, amount)])

    def cancel(self, link_id: str) -> None:
        self.cancelled.append(link_id)


class RazorpayExecutor:
    """Real test-mode calls. Holds the only copy of the Razorpay credentials in the process."""

    def __init__(self, key_id: str, key_secret: str, clock: Callable[[], int] | None = None) -> None:
        import razorpay  # imported here so tests never need the SDK loaded

        self.client = razorpay.Client(auth=(key_id, key_secret))
        self._clock = clock or (lambda: int(time.time()))

    def create_payment_link(self, pm: PaymentMandate, description: str, notes: dict) -> LinkInfo:
        data = self.client.payment_link.create(
            {
                "amount": pm.amount_paise,
                "currency": pm.currency,
                "reference_id": pm.payment_id[:40],
                "description": description[:2048],
                "expire_by": self._clock() + LINK_TTL_S,
                "notes": {str(k)[:40]: str(v)[:256] for k, v in notes.items()},
                "notify": {"sms": False, "email": False},
                "reminder_enable": False,
            }
        )
        return LinkInfo(data["id"], data["short_url"], data["status"])

    def poll(self, link_id: str, timeout_s: int, interval_s: float, seen: set[str]) -> PollResult:
        deadline = time.monotonic() + timeout_s
        while True:
            data = self.client.payment_link.fetch(link_id)
            attempts = self._attempts_for(data)
            if data.get("status") == "paid":
                paid = next((a for a in attempts if a.status in PAID_STATUSES), None)
                amount = int(data.get("amount_paid", 0)) or (paid.amount_paise if paid else 0)
                return PollResult("paid", paid.payment_id if paid else None, amount, attempts)
            for a in attempts:
                if a.status == "failed" and a.payment_id not in seen:
                    seen.add(a.payment_id)
                    return PollResult("failed", a.payment_id, a.amount_paise, attempts)
            if time.monotonic() >= deadline:
                return PollResult("timeout", attempts=attempts)
            time.sleep(interval_s)

    def _attempts_for(self, link: dict) -> list[Attempt]:
        """Every payment attempt against this link.

        The link's own `payments` array lists only captured payments, so failed attempts come from the
        Payments API, matched by the link's order_id or by the payment-mandate id in the link's notes.
        """
        wanted_pm = (link.get("notes") or {}).get("payment_id")
        order_id = link.get("order_id")
        by_id: dict[str, Attempt] = {}
        for p in link.get("payments") or []:
            pid = str(p.get("payment_id", ""))
            by_id[pid] = Attempt(pid, str(p.get("status", "")), int(p.get("amount", 0)))
        for p in self.client.payment.all({"count": 25}).get("items", []):
            matches_order = order_id is not None and p.get("order_id") == order_id
            matches_notes = wanted_pm is not None and (p.get("notes") or {}).get("payment_id") == wanted_pm
            if (matches_order or matches_notes) and p["id"] not in by_id:
                by_id[p["id"]] = Attempt(p["id"], str(p.get("status", "")), int(p.get("amount", 0)))
        return sorted(by_id.values(), key=lambda a: a.payment_id)

    def cancel(self, link_id: str) -> None:
        self.client.payment_link.cancel(link_id)
