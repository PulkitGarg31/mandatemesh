import pytest

from mandatemesh.executor import FakeExecutor, RazorpayExecutor
from mandatemesh.mandates import PaymentMandate

PM = PaymentMandate("pm_1", "im_1", "cm_1", 91_000, "INR", 1_800_000_000)


def test_fake_create_returns_link_and_tracks_amount():
    ex = FakeExecutor(["paid"])
    link = ex.create_payment_link(PM, "desc", {"payment_id": "pm_1"})
    assert link.link_id == "plink_fake001" and link.short_url.startswith("https://") and link.status == "created"
    assert ex.links == [link] and ex.amounts[link.link_id] == 91_000


def test_fake_poll_follows_script_and_tracks_seen():
    ex = FakeExecutor(["failed", "paid"])
    link = ex.create_payment_link(PM, "d", {})
    seen: set[str] = set()
    r1 = ex.poll(link.link_id, 1, 0, seen)
    assert r1.outcome == "failed" and r1.attempts[0].status == "failed" and r1.payment_id in seen
    r2 = ex.poll(link.link_id, 1, 0, seen)
    assert r2.outcome == "paid" and r2.amount_paise == 91_000 and r2.attempts[0].status == "captured"
    assert len(seen) == 2


def test_fake_poll_times_out_when_script_is_empty():
    ex = FakeExecutor([])
    link = ex.create_payment_link(PM, "d", {})
    assert ex.poll(link.link_id, 1, 0, set()).outcome == "timeout"


def test_fake_default_is_one_paid_outcome():
    ex = FakeExecutor()
    link = ex.create_payment_link(PM, "d", {})
    assert ex.poll(link.link_id, 1, 0, set()).outcome == "paid"


def test_fake_cancel_records():
    ex = FakeExecutor()
    link = ex.create_payment_link(PM, "d", {})
    ex.cancel(link.link_id)
    assert ex.cancelled == [link.link_id]


def _executor_with(link_states, order_items=None, all_items=None):
    ex = RazorpayExecutor.__new__(RazorpayExecutor)  # skip __init__: no client construction
    states = iter(link_states)

    class FakeLinks:
        def fetch(self, link_id, **kw):
            return next(states)

    class FakeOrders:
        def payments(self, order_id, **kw):
            return {"items": list(order_items or [])}

    class FakePayments:
        def all(self, data, **kw):
            return {"items": list(all_items or [])}

    class FakeClient:
        payment_link = FakeLinks()
        order = FakeOrders()
        payment = FakePayments()

    ex.client = FakeClient()
    return ex


def test_razorpay_poll_finds_failed_attempt_via_order_payments_without_network():
    ex = _executor_with(
        [
            {"status": "created", "order_id": "order_1", "notes": {"payment_id": "pm_1"}, "payments": None},
            {"status": "paid", "order_id": "order_1", "notes": {"payment_id": "pm_1"}, "amount_paid": 91000,
             "payments": [{"payment_id": "pay_ok", "status": "captured", "amount": 91000}]},
        ],
        order_items=[{"id": "pay_f1", "status": "failed", "amount": 91000, "order_id": "order_1"}],
        all_items=[{"id": "pay_other", "status": "failed", "amount": 500, "order_id": "order_zzz", "notes": {"payment_id": "pm_1"}}],
    )
    seen: set[str] = set()
    r1 = ex.poll("plink_x", 5, 0, seen)
    assert r1.outcome == "failed" and r1.payment_id == "pay_f1" and seen == {"pay_f1"}
    r2 = ex.poll("plink_x", 5, 0, seen)
    assert r2.outcome == "paid" and r2.payment_id == "pay_ok" and r2.amount_paise == 91000
    assert {a.payment_id for a in r2.attempts} == {"pay_f1", "pay_ok"}


def test_razorpay_attempts_match_by_notes_when_order_id_missing():
    ex = RazorpayExecutor.__new__(RazorpayExecutor)

    class FakePayments:
        def all(self, data, **kw):
            return {"items": [{"id": "pay_n", "status": "failed", "amount": 1, "order_id": None, "notes": {"payment_id": "pm_9"}}]}

    class FakeClient:
        payment = FakePayments()

    ex.client = FakeClient()
    attempts = ex._attempts_for({"notes": {"payment_id": "pm_9"}, "payments": None})
    assert [a.payment_id for a in attempts] == ["pay_n"] and attempts[0].status == "failed"


def test_razorpay_second_failure_is_detected_with_seen_prepopulated():
    ex = _executor_with(
        [{"status": "created", "order_id": "order_1", "notes": {}, "payments": None}],
        order_items=[{"id": "pay_f1", "status": "failed", "amount": 91000}, {"id": "pay_f2", "status": "failed", "amount": 91000}],
    )
    seen = {"pay_f1"}
    r = ex.poll("plink_x", 5, 0, seen)
    assert r.outcome == "failed" and r.payment_id == "pay_f2" and seen == {"pay_f1", "pay_f2"}


def test_razorpay_paid_on_first_fetch_falls_back_to_link_amount():
    ex = _executor_with([{"status": "paid", "order_id": "order_1", "notes": {}, "amount_paid": 0, "amount": 91000, "payments": None}])
    r = ex.poll("plink_x", 5, 0, set())
    assert r.outcome == "paid" and r.payment_id is None and r.amount_paise == 91000


def test_razorpay_dead_link_returns_timeout_immediately():
    ex = _executor_with([{"status": "cancelled", "order_id": None, "notes": {}, "payments": None}])
    assert ex.poll("plink_x", 60, 0, set()).outcome == "timeout"


def test_razorpay_poll_tolerates_transient_errors_then_succeeds():
    ex = RazorpayExecutor.__new__(RazorpayExecutor)
    calls = {"n": 0}

    class FakeLinks:
        def fetch(self, link_id, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("blip")
            return {"status": "paid", "order_id": None, "notes": {}, "amount_paid": 5, "payments": None}

    class FakeClient:
        payment_link = FakeLinks()

    ex.client = FakeClient()
    r = ex.poll("plink_x", 5, 0, set())
    assert r.outcome == "paid" and calls["n"] == 3


def test_razorpay_executor_refuses_live_key():
    with pytest.raises(ValueError, match="TEST keys"):
        RazorpayExecutor("rzp_live_abc", "secret")
