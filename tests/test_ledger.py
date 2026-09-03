import json

from mandatemesh.ledger import GENESIS_HASH, Ledger, compute_hash, tamper


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


def test_spent_for_subtracts_refunds_and_counts_chain_ids(tmp_path):
    l = make(tmp_path)
    l.append("payment.captured", "executor", {"intent_id": "im_1", "chain_ids": ["im_1", "sm_1"], "payment_id": "pm_1", "amount_paise": 91_000})
    l.append("refund.created", "gate", {"intent_id": "im_1", "payment_id": "pm_1", "amount_paise": 14_000})
    assert l.spent_for("im_1") == 77_000
    assert l.spent_for("sm_1") == 77_000
    assert l.spent_for("sm_9") == 0


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


def test_deleted_line_is_detected_even_if_renumbered(tmp_path):
    l = make(tmp_path)
    for i in range(5):
        l.append("x", "a", {"i": i})
    lines = l.path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    l.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert make(tmp_path).verify() == (False, 2)
    rows = [json.loads(x) for x in lines]
    for pos, row in enumerate(rows):
        row["seq"] = pos
    l.path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    assert make(tmp_path).verify() == (False, 2)


def test_rehashed_edit_moves_the_break_to_the_next_seq(tmp_path):
    l = make(tmp_path)
    for i in range(4):
        l.append("x", "a", {"i": i})
    lines = l.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["payload"]["i"] = 99
    row["hash"] = compute_hash(row["prev_hash"], {k: v for k, v in row.items() if k != "hash"})
    lines[1] = json.dumps(row)
    l.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert make(tmp_path).verify() == (False, 2)


def test_receipt_picks_the_right_capture_for_a_second_payment(tmp_path):
    l = make(tmp_path)
    for n, pay in (("1", "pay_FIRST"), ("2", "pay_SECOND")):
        l.append("mandate.payment.created", "gate", {"intent_id": "im_1", "cart_id": f"cm_{n}", "payment_id": f"pm_{n}", "amount_paise": 1000})
        l.append("payment.captured", "executor", {"intent_id": "im_1", "cart_id": f"cm_{n}", "payment_id": f"pm_{n}", "razorpay_payment_id": pay, "amount_paise": 1000})
    text = l.receipt("pm_2")
    assert "pay_SECOND" in text and "pay_FIRST" not in text
    assert "Payment attempts: 1" in text


def test_append_copies_payload_so_later_mutation_cannot_drift_memory_from_disk(tmp_path):
    l = make(tmp_path)
    payload = {"intent_id": "im_1", "amount_paise": 100}
    l.append("payment.captured", "executor", payload)
    payload["amount_paise"] = 99_999
    assert l.verify() == (True, None)
    assert l.spent_for("im_1") == 100


def test_corrupt_line_reports_broken_instead_of_raising(tmp_path):
    l = make(tmp_path)
    l.append("a", "x", {"n": 1})
    l.append("b", "x", {"n": 2})
    with l.path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "truncated')
    assert make(tmp_path).verify() == (False, 2)


def test_receipt_reports_a_broken_chain(tmp_path):
    l = make(tmp_path)
    l.append("mandate.payment.created", "gate", {"intent_id": "im_1", "cart_id": "cm_1", "payment_id": "pm_1", "amount_paise": 1})
    l.append("payment.captured", "executor", {"intent_id": "im_1", "cart_id": "cm_1", "payment_id": "pm_1", "razorpay_payment_id": None, "amount_paise": 1})
    assert "Chain: verified" in l.receipt("pm_1") and "payment id not reported" in l.receipt("pm_1")
    tamper(l.path, 0)
    assert "Chain: BROKEN at seq 0" in make(tmp_path).receipt("pm_1")
