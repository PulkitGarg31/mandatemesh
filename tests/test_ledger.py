from mandatemesh.ledger import GENESIS_HASH, Ledger, tamper


def make(tmp_path, clock=None):
    return Ledger(tmp_path / "ledger.jsonl", clock=clock or (lambda: 1_800_000_000))


def test_first_event_links_to_genesis(tmp_path):
    l = make(tmp_path)
    e = l.append("mandate.intent.created", "user", {"intent_id": "im_1"})
    assert e.seq == 0 and e.prev_hash == GENESIS_HASH and len(e.hash) == 64
    assert e.ts == 1_800_000_000 and e.id.startswith("evt_")


def test_chain_verifies_and_reloads_from_disk(tmp_path):
    l = make(tmp_path)
    for i in range(3):
        l.append("x", "a", {"i": i})
    assert l.verify() == (True, None)
    again = make(tmp_path)
    assert [e.hash for e in again.events()] == [e.hash for e in l.events()]
    assert again.verify() == (True, None)
    assert again.head_hash == l.events()[-1].hash


def test_tamper_is_detected_at_the_edited_seq(tmp_path):
    l = make(tmp_path)
    l.append("a", "x", {"n": 1})
    l.append("payment.captured", "executor", {"amount_paise": 100, "intent_id": "im_1"})
    l.append("c", "x", {"n": 3})
    tamper(l.path, 1)
    assert make(tmp_path).verify() == (False, 1)


def test_spent_for_sums_only_captured_for_that_intent(tmp_path):
    l = make(tmp_path)
    l.append("payment.captured", "executor", {"intent_id": "im_1", "amount_paise": 91_000})
    l.append("payment.failed", "executor", {"intent_id": "im_1", "amount_paise": 50_000})
    l.append("payment.captured", "executor", {"intent_id": "im_2", "amount_paise": 10_000})
    l.append("payment.captured", "executor", {"intent_id": "im_1", "amount_paise": 9_000})
    assert l.spent_for("im_1") == 100_000
    assert l.spent_for("im_9") == 0


def test_receipt_collects_related_events(tmp_path):
    l = make(tmp_path)
    l.append("mandate.intent.created", "user", {"intent_id": "im_1"})
    l.append("gate.decision", "gate", {"intent_id": "im_1", "cart_id": "cm_1", "verdict": "ALLOW", "rule_id": "ALLOW", "reason": "ok",
                                        "checks": [{"rule_id": "R01_AGENT_REGISTERED", "passed": True, "detail": "registered"}]})
    l.append("mandate.payment.created", "gate", {"intent_id": "im_1", "cart_id": "cm_1", "payment_id": "pm_1", "amount_paise": 91_000})
    l.append("unrelated", "x", {"intent_id": "im_2"})
    l.append("payment.captured", "executor", {"intent_id": "im_1", "payment_id": "pm_1", "razorpay_payment_id": "pay_X", "amount_paise": 91_000})
    text = l.receipt("pm_1")
    assert "pm_1" in text and "pay_X" in text and "R01_AGENT_REGISTERED" in text
    assert "unrelated" not in text
    assert l.head_hash in text
