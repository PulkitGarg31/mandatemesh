import pytest

from mandatemesh.agent import ScriptedAgent
from mandatemesh.executor import FakeExecutor
from mandatemesh.fixtures import AGENT_ID, FIXED_NOW, MERCHANT_ID
from mandatemesh.keys import Keys
from mandatemesh.ledger import Ledger
from mandatemesh.mandates import MalformedMandate
from mandatemesh.merchant import MockMerchant
import mandatemesh.orchestrator as orch_mod
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


def test_scenarios_table_has_the_seven_demos():
    assert set(SCENARIOS) == {"happy", "stepup", "payfail", "poison", "revoke", "delegate", "overreach"}
    assert SCENARIOS["revoke"].revoke_before_proposal and not SCENARIOS["happy"].revoke_before_proposal
    assert SCENARIOS["happy"].delegation is None
    assert len(SCENARIOS["delegate"].delegation) == 1 and len(SCENARIOS["overreach"].delegation) == 1


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
    assert types(ledger)[-1] == "orchestrator.replay_refused"
    assert ledger.verify() == (True, None)


class _CancelRaises(FakeExecutor):
    def cancel(self, link_id):
        raise RuntimeError("payment link is already paid")


class _PollRaises(FakeExecutor):
    def poll(self, link_id, timeout_s, interval_s, seen):
        raise RuntimeError("BadRequestError: bad link")


class _CreateRaises(FakeExecutor):
    def create_payment_link(self, pm, description, notes):
        raise RuntimeError("network down")


def test_link_creation_failure_is_an_error_outcome(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy")
    orch.executor = _CreateRaises()
    r = orch.run(sc)
    assert r.outcome == "error" and r.link is None
    assert types(ledger)[-1] == "razorpay.link.failed" and ledger.verify() == (True, None)


def test_cancel_failure_with_late_capture_is_recorded_as_paid(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "payfail")
    orch.executor = _CancelRaises(["failed", "failed", "paid"])
    r = orch.run(sc)
    assert r.outcome == "paid"
    t = types(ledger)
    assert "razorpay.link.cancelled" not in t and t[-1] == "payment.captured"
    assert ledger.spent_for(r.intent_id) == 91_000


def test_cancel_failure_without_capture_is_honest(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "payfail")
    orch.executor = _CancelRaises(["failed", "failed"])
    r = orch.run(sc)
    assert r.outcome == "abandoned"
    t = types(ledger)
    assert "razorpay.link.cancelled" not in t and t[-2:] == ["razorpay.link.cancel_failed", "payment.abandoned"]


def test_poll_exception_closes_the_link_and_reports_error(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy")
    orch.executor = _PollRaises()
    r = orch.run(sc)
    assert r.outcome == "error" and r.link is not None
    t = types(ledger)
    assert "payment.error" in t and t[-1] == "razorpay.link.cancelled"
    assert orch.executor.cancelled == [r.link.link_id]


def test_approver_sees_cart_and_decision_and_token_is_reused_on_retry(tmp_path):
    seen_args = []

    def approver(cart, decision):
        seen_args.append((cart.total_paise, decision.verdict, decision.rule_id))
        return True

    orch, sc, ex, ledger = build(tmp_path, "stepup", outcomes=("failed", "paid"))
    orch.approver = approver
    r = orch.run(sc)
    assert r.outcome == "paid"
    assert seen_args == [(180_000, "STEP_UP", "R14_PER_TXN_CAP")]
    assert [d.payload["verdict"] for d in ledger.of_type("gate.decision")] == ["STEP_UP", "ALLOW", "ALLOW"]


class _PollInterrupts(FakeExecutor):
    def poll(self, link_id, timeout_s, interval_s, seen):
        raise KeyboardInterrupt


def test_keyboard_interrupt_closes_the_link_then_propagates(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy")
    orch.executor = _PollInterrupts()
    with pytest.raises(KeyboardInterrupt):
        orch.run(sc)
    t = [e.type for e in ledger.events()]
    assert t[-2:] == ["payment.error", "razorpay.link.cancelled"]
    assert orch.executor.cancelled


def test_delegate_scenario_pays_with_chain(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "delegate")
    r = orch.run(sc)
    assert r.outcome == "paid"
    t = types(ledger)
    assert t.count("agent.registered") == 2 and t.count("mandate.sub.created") == 1
    sub = ledger.of_type("mandate.sub.created")[0].payload
    assert sub["parent_id"] == r.intent_id and sub["delegator_id"] == "planner-01" and sub["agent_id"] == AGENT_ID
    decisions = ledger.of_type("gate.decision")
    assert len(decisions) == 1
    d = decisions[0].payload
    assert d["verdict"] == "ALLOW"
    assert d["chain_ids"] == [r.intent_id, sub["sub_id"]]
    assert d["now"] == FIXED_NOW and d["spent_paise"] == 0
    assert d["spent_by"] == {sub["sub_id"]: 0} and d["stepup_id"] is None
    assert ledger.of_type("payment.captured")[0].payload["chain_ids"] == [r.intent_id, sub["sub_id"]]
    r18 = [c for c in d["checks"] if c["rule_id"] == "R18_DELEGATION_CHAIN"]
    assert len(r18) == 1 and r18[0]["passed"] and "planner-01 -> shopper-01" in r18[0]["detail"]


def test_overreach_scenario_is_denied_on_r19(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "overreach")
    r = orch.run(sc)
    assert r.outcome == "denied" and r.decision.rule_id == "R19_DELEGATION_SUBSET" and ex.links == []
    assert "mandate.sub.created" in types(ledger) and "mandate.payment.created" not in types(ledger)


def test_spent_for_counts_the_sub_link(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "delegate")
    r = orch.run(sc)
    sub_id = ledger.of_type("mandate.sub.created")[0].payload["sub_id"]
    assert ledger.spent_for(sub_id) == 91_000
    assert ledger.spent_for(r.intent_id) == 91_000


def test_intent_event_carries_user_pubkey_and_cart_event_merchant_pubkey(tmp_path):
    orch, sc, ex, ledger = build(tmp_path, "happy")
    r = orch.run(sc)
    assert r.outcome == "paid"
    assert ledger.of_type("mandate.intent.created")[0].payload["user_pubkey"] == orch.keys.pub("user")
    assert ledger.of_type("merchant.cart.quoted")[0].payload["merchant_pubkey"] == orch.merchant.pubkey_b64


def test_malformed_cart_is_a_quote_rejection_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(orch_mod.CartMandate, "from_payload", classmethod(lambda cls, p: (_ for _ in ()).throw(MalformedMandate("bad cart"))))
    orch, sc, ex, ledger = build(tmp_path, "happy")
    r = orch.run(sc)
    assert r.outcome == "quote_rejected" and ex.links == []
    assert [e.type for e in ledger.events()][-1] == "merchant.quote.rejected"
