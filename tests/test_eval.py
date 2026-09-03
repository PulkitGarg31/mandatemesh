from mandatemesh.evalset import build_cases, run_eval


def test_eval_set_shape():
    cases = build_cases()
    assert len(cases) == 12
    assert sum(c.expect_blocked for c in cases) == 8


def test_gate_blocks_all_poisoned_and_passes_all_benign():
    m = run_eval()
    assert m["poisoned"] == 8 and m["benign"] == 4
    assert m["block_rate"] == 1.0 and m["false_positive_rate"] == 0.0
    assert all(r.correct for r in m["rows"])
    by_name = {r.name: r for r in m["rows"]}
    assert by_name["injection_over_quantity"].rule_id == "R14_PER_TXN_CAP"
    assert by_name["revoked_agent"].rule_id == "R02_AGENT_ACTIVE"
    assert by_name["forged_proposal_signature"].rule_id == "R03_PROPOSAL_SIG"
