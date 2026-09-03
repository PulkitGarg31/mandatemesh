from mandatemesh.agent import ScriptedAgent
from mandatemesh.executor import FakeExecutor
from mandatemesh.fixtures import AGENT_ID, FIXED_NOW, MERCHANT_ID
from mandatemesh.keys import Keys
from mandatemesh.ledger import Ledger
from mandatemesh.merchant import MockMerchant
from mandatemesh.orchestrator import SCENARIOS, Orchestrator
from mandatemesh.registry import AgentRegistry


def build(tmp_path, scenario_name, outcomes=("paid",), approve=True, proposals=None):
    sc = SCENARIOS[scenario_name]
    keys = Keys.generate()
    clock = lambda: FIXED_NOW  # noqa: E731
    agent = ScriptedAgent(AGENT_ID, keys.agent, list(sc.scripted_items if proposals is None else proposals), clock=clock)
    executor = FakeExecutor(list(outcomes))
    ledger = Ledger(tmp_path / "ledger.jsonl", clock=clock)
    orch = Orchestrator(
        keys, AgentRegistry(), MockMerchant(MERCHANT_ID, keys.merchant, clock=clock), agent, executor, ledger,
        approver=lambda cart, decision: approve, say=lambda s: None, clock=clock, poll_timeout_s=1, poll_interval_s=0,
    )
    return orch, sc, executor, ledger


def types(ledger):
    return [e.type for e in ledger.events()]


def test_scenarios_table_has_the_five_demos():
    assert set(SCENARIOS) == {"happy", "stepup", "payfail", "poison", "revoke"}
    assert SCENARIOS["revoke"].revoke_before_proposal and not SCENARIOS["happy"].revoke_before_proposal


def test_happy_path_pays_and_ledger_verifies(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy")
    r = orch.run(sc)
    assert r.outcome == "paid" and r.razorpay_payment_id == "pay_fake001" and r.link.link_id == "plink_fake001"
    assert types(ledger) == [
        "mandate.intent.created", "agent.registered", "agent.proposal", "merchant.cart.quoted",
        "gate.decision", "mandate.payment.created", "razorpay.link.created", "payment.captured",
    ]
    assert ledger.verify() == (True, None)
    assert ledger.spent_for(r.intent_id) == 91_000
    assert "pay_fake001" in ledger.receipt(r.payment_id)


def test_failed_payment_retries_once_then_succeeds(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "payfail", outcomes=("failed", "paid"))
    r = orch.run(sc)
    assert r.outcome == "paid"
    t = types(ledger)
    assert t.count("payment.failed") == 1 and t.count("payment.retry") == 1 and t.count("gate.decision") == 2
    assert t[-1] == "payment.captured" and ex.cancelled == []


def test_two_failures_abandon_and_cancel_link(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "payfail", outcomes=("failed", "failed"))
    r = orch.run(sc)
    assert r.outcome == "abandoned"
    t = types(ledger)
    assert t.count("payment.failed") == 2 and t[-2:] == ["razorpay.link.cancelled", "payment.abandoned"]
    assert ex.cancelled == [r.link.link_id]
    assert ledger.spent_for(r.intent_id) == 0


def test_timeout_counts_as_a_failed_attempt(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy", outcomes=())
    r = orch.run(sc)
    assert r.outcome == "abandoned" and types(ledger).count("payment.timeout") == 2


def test_stepup_approved_then_paid(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "stepup", approve=True)
    r = orch.run(sc)
    assert r.outcome == "paid"
    t = types(ledger)
    assert "stepup.requested" in t and "stepup.approved" in t
    decisions = ledger.of_type("gate.decision")
    assert [d.payload["verdict"] for d in decisions] == ["STEP_UP", "ALLOW"]
    assert decisions[0].payload["rule_id"] == "R14_PER_TXN_CAP"


def test_stepup_declined_creates_nothing(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "stepup", approve=False)
    r = orch.run(sc)
    assert r.outcome == "declined" and ex.links == []
    assert types(ledger)[-1] == "stepup.declined"


def test_poison_scripted_is_blocked(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "poison", approve=False)
    r = orch.run(sc)
    assert r.outcome == "declined" and ex.links == []
    assert ledger.of_type("gate.decision")[0].payload["rule_id"] == "R14_PER_TXN_CAP"


def test_revoked_agent_is_denied(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "revoke")
    r = orch.run(sc)
    assert r.outcome == "denied" and r.decision.rule_id == "R02_AGENT_ACTIVE" and ex.links == []
    assert "agent.revoked" in types(ledger)


def test_no_proposal_is_logged(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy", proposals=[])
    r = orch.run(sc)
    assert r.outcome == "no_proposal" and types(ledger)[-1] == "agent.no_proposal"
    assert ledger.of_type("agent.no_proposal")[0].payload["reason"] == "script exhausted"


def test_replay_of_an_already_paid_cart_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr("mandatemesh.merchant.new_id", lambda prefix: "cm_fixed")
    orch, sc, ex, ledger = build(tmp_path, "happy")
    ledger.append("payment.captured", "executor", {"intent_id": "im_old", "cart_id": "cm_fixed", "payment_id": "pm_old", "amount_paise": 91_000})
    r = orch.run(sc)
    assert r.outcome == "denied" and r.decision is None and ex.links == []
    assert types(ledger)[-1] == "gate.replay_refused"
    assert ledger.verify() == (True, None)
