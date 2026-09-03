from mandatemesh.cli import main


def test_eval_command_exits_zero():
    assert main(["eval"]) == 0


def test_keys_init_then_scripted_fake_demo_and_ledger_commands(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKE_OUTCOMES", raising=False)
    assert main(["keys", "init"]) == 0
    assert main(["keys", "init"]) == 1  # refuses to overwrite without --force
    assert main(["demo", "--scenario", "happy", "--agent", "scripted", "--executor", "fake", "--run-id", "t1"]) == 0
    ledger = tmp_path / "runs" / "t1" / "ledger.jsonl"
    assert ledger.exists()
    assert list((tmp_path / "runs" / "t1").glob("receipt-pm_*.md"))
    assert main(["ledger", "verify", str(ledger)]) == 0
    assert main(["ledger", "tamper", str(ledger), "3"]) == 0
    assert main(["ledger", "verify", str(ledger)]) == 2
    assert main(["ledger", "verify", str(tmp_path / "runs" / "nope" / "ledger.jsonl")]) == 2


def test_stepup_auto_approve_and_declined_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKE_OUTCOMES", raising=False)
    assert main(["keys", "init"]) == 0
    assert main(["demo", "--scenario", "stepup", "--agent", "scripted", "--executor", "fake", "--auto-approve", "yes", "--run-id", "s1"]) == 0
    assert main(["demo", "--scenario", "poison", "--agent", "scripted", "--executor", "fake", "--auto-approve", "no", "--run-id", "p1"]) == 0
    assert main(["demo", "--scenario", "revoke", "--agent", "scripted", "--executor", "fake", "--run-id", "r1"]) == 0
    assert main(["demo", "--scenario", "payfail", "--agent", "scripted", "--executor", "fake", "--run-id", "f1"]) == 0
