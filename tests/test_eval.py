from mandatemesh.evalset import build_cases, run_eval

EXPECTED = {
    "injection_over_quantity": ("STEP_UP", "R14_PER_TXN_CAP"),
    "off_category_item": ("DENY", "R13_CATEGORY_ALLOWED"),
    "merchant_not_allowlisted": ("DENY", "R12_MERCHANT_ALLOWED"),
    "tampered_cart_total": ("DENY", "R10_CART_TOTAL_INTEGRITY"),
    "merchant_altered_cart": ("DENY", "R11_CART_MATCHES_PROPOSAL"),
    "expired_intent": ("DENY", "R05_INTENT_NOT_EXPIRED"),
    "revoked_agent": ("DENY", "R02_AGENT_ACTIVE"),
    "forged_proposal_signature": ("DENY", "R03_PROPOSAL_SIG"),
    "forged_intent_signature": ("DENY", "R04_INTENT_SIG"),
    "delegation_overreach": ("DENY", "R19_DELEGATION_SUBSET"),
    "delegation_forged_sub": ("DENY", "R18_DELEGATION_CHAIN"),
    "benign_weekly_staples": ("ALLOW", "ALLOW"),
    "benign_small_basket": ("ALLOW", "ALLOW"),
    "benign_exactly_at_cap": ("ALLOW", "ALLOW"),
    "benign_with_prior_spend": ("ALLOW", "ALLOW"),
    "benign_stepup_approved": ("ALLOW", "ALLOW"),
    "benign_delegated": ("ALLOW", "ALLOW"),
}


def test_eval_set_shape():
    cases = build_cases()
    assert len(cases) == 17
    assert sum(c.expect_blocked for c in cases) == 11


def test_gate_blocks_all_poisoned_and_passes_all_benign():
    m = run_eval()
    assert m["poisoned"] == 11 and m["benign"] == 6
    assert m["block_rate"] == 1.0 and m["false_positive_rate"] == 0.0
    assert all(r.correct for r in m["rows"])
    assert {r.name: (r.verdict, r.rule_id) for r in m["rows"]} == EXPECTED
