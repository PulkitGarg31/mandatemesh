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


def test_razorpay_poll_finds_failed_attempt_via_payments_api_without_network():
    ex = RazorpayExecutor.__new__(RazorpayExecutor)  # skip __init__: no client construction
    link_states = iter([
        {"status": "created", "order_id": "order_1", "notes": {"payment_id": "pm_1"}, "payments": None},
        {"status": "paid", "order_id": "order_1", "notes": {"payment_id": "pm_1"}, "amount_paid": 91000,
         "payments": [{"payment_id": "pay_ok", "status": "captured", "amount": 91000}]},
    ])
    payments_api = [
        {"id": "pay_f1", "status": "failed", "amount": 91000, "order_id": "order_1", "notes": {"payment_id": "pm_1"}},
        {"id": "pay_other", "status": "failed", "amount": 500, "order_id": "order_zzz", "notes": {}},
    ]

    class FakeLinks:
        def fetch(self, link_id):
            return next(link_states)

    class FakePayments:
        def all(self, data):
            return {"items": payments_api}

    class FakeClient:
        payment_link = FakeLinks()
        payment = FakePayments()

    ex.client = FakeClient()
    seen: set[str] = set()
    r1 = ex.poll("plink_x", 5, 0, seen)
    assert r1.outcome == "failed" and r1.payment_id == "pay_f1" and seen == {"pay_f1"}
    r2 = ex.poll("plink_x", 5, 0, seen)
    assert r2.outcome == "paid" and r2.payment_id == "pay_ok" and r2.amount_paise == 91000
    assert {a.payment_id for a in r2.attempts} == {"pay_f1", "pay_ok"}


def test_razorpay_attempts_match_by_notes_when_order_id_missing():
    ex = RazorpayExecutor.__new__(RazorpayExecutor)

    class FakePayments:
        def all(self, data):
            return {"items": [{"id": "pay_n", "status": "failed", "amount": 1, "order_id": None, "notes": {"payment_id": "pm_9"}}]}

    class FakeClient:
        payment = FakePayments()

    ex.client = FakeClient()
    attempts = ex._attempts_for({"notes": {"payment_id": "pm_9"}, "payments": None})
    assert [a.payment_id for a in attempts] == ["pay_n"] and attempts[0].status == "failed"
