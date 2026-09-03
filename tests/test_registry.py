from mandatemesh.registry import ACTIVE, REVOKED, AgentRegistry


def test_register_and_lookup():
    r = AgentRegistry()
    rec = r.register("shopper-01", "PUBKEY")
    assert rec.status == ACTIVE
    assert r.get("shopper-01").pubkey_b64 == "PUBKEY"
    assert r.is_active("shopper-01")


def test_unknown_agent_is_not_active():
    r = AgentRegistry()
    assert r.get("ghost") is None
    assert not r.is_active("ghost")


def test_revoke_flips_status_and_keeps_key():
    r = AgentRegistry()
    r.register("shopper-01", "PUBKEY")
    r.revoke("shopper-01")
    assert r.get("shopper-01").status == REVOKED
    assert r.get("shopper-01").pubkey_b64 == "PUBKEY"
    assert not r.is_active("shopper-01")
