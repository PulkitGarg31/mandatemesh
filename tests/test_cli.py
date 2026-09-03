import json

import pytest

import mandatemesh.cli as cli_mod
from mandatemesh.cli import main
from mandatemesh.ledger import Ledger
from mandatemesh.merchant import DEFAULT_FEED, MockMerchant as RealMerchant


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
    monkeypatch.setenv("FAKE_OUTCOMES", "failed,paid")
    assert main(["demo", "--scenario", "payfail", "--agent", "scripted", "--executor", "fake", "--run-id", "f1"]) == 0
    assert "payment.retry" in [e.type for e in Ledger(tmp_path / "runs" / "f1" / "ledger.jsonl").events()]


def test_delegation_scenarios_run_offline(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKE_OUTCOMES", raising=False)
    assert main(["keys", "init"]) == 0
    assert main(["demo", "--scenario", "delegate", "--agent", "scripted", "--executor", "fake", "--run-id", "d1"]) == 0
    assert main(["demo", "--scenario", "overreach", "--agent", "scripted", "--executor", "fake", "--run-id", "o1"]) == 0
    assert "mandate.sub.created" in [e.type for e in Ledger(tmp_path / "runs" / "d1" / "ledger.jsonl").events()]


def test_refund_scenario_runs_offline_and_records_the_refund(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKE_OUTCOMES", raising=False)
    assert main(["keys", "init"]) == 0
    assert main(["demo", "--scenario", "refund", "--agent", "scripted", "--executor", "fake", "--run-id", "r1"]) == 0
    types = [e.type for e in Ledger(tmp_path / "runs" / "r1" / "ledger.jsonl").events()]
    assert "refund.created" in types and "merchant.shortfall" in types
    assert "recorded" in capsys.readouterr().out


def test_cli_errors_are_clean_exit_codes(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAKE_OUTCOMES", raising=False)
    assert main(["demo", "--scenario", "happy", "--agent", "scripted", "--executor", "fake", "--run-id", "nokeys"]) == 2  # no keys/ yet
    assert "keys init" in capsys.readouterr().out
    assert main(["keys", "init"]) == 0
    assert main(["demo", "--scenario", "happy", "--agent", "scripted", "--executor", "fake", "--run-id", "x[red]y"]) == 0
    out = capsys.readouterr().out
    assert "x[red]y" in out
    ledger = tmp_path / "runs" / "x[red]y" / "ledger.jsonl"
    assert main(["ledger", "receipt", str(ledger), "pm_typo"]) == 2
    assert main(["ledger", "tamper", str(ledger), "99"]) == 2
    assert main(["ledger", "verify", str(tmp_path / "runs" / "x[red]y")]) == 2  # a directory, not a file


def test_run_id_must_be_a_plain_name(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["keys", "init"]) == 0
    with pytest.raises(SystemExit):
        main(["demo", "--scenario", "happy", "--agent", "scripted", "--executor", "fake", "--run-id", "../escape"])


def test_bad_feed_is_a_clean_error_exit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["keys", "init"]) == 0
    feed = json.loads(DEFAULT_FEED.read_text(encoding="utf-8"))
    feed["items"][0]["price_paise"] = 450.0
    bad = tmp_path / "feed.json"
    bad.write_text(json.dumps(feed), encoding="utf-8")

    def merchant_with_bad_feed(merchant_id, key):
        return RealMerchant(merchant_id, key, feed_path=bad)

    monkeypatch.setattr(cli_mod, "MockMerchant", merchant_with_bad_feed)
    assert main(["demo", "--scenario", "happy", "--agent", "scripted", "--executor", "fake", "--run-id", "badfeed"]) == 2
