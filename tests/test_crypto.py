import json
import os
import stat

import pytest

from mandatemesh.crypto import (
    Envelope,
    canonical_json,
    generate_private_key,
    load_private_key,
    public_b64,
    save_private_key,
    sign,
    verify,
)


def test_canonical_json_is_sorted_and_compact():
    assert canonical_json({"b": 2, "a": [1, {"d": 4, "c": 3}]}) == b'{"a":[1,{"c":3,"d":4}],"b":2}'


def test_sign_verify_round_trip():
    key = generate_private_key()
    env = sign({"b": 2, "a": 1}, key, "user")
    assert env.signer == "user"
    assert env.alg == "Ed25519"
    assert verify(env, public_b64(key))


def test_tampered_payload_fails():
    key = generate_private_key()
    env = sign({"amount": 100}, key, "user")
    bad = Envelope(payload={"amount": 100000}, signer=env.signer, sig=env.sig)
    assert not verify(bad, public_b64(key))


def test_wrong_key_fails():
    env = sign({"x": 1}, generate_private_key(), "user")
    assert not verify(env, public_b64(generate_private_key()))


def test_garbage_signature_fails_closed():
    key = generate_private_key()
    env = Envelope(payload={"x": 1}, signer="user", sig="not-base64!!")
    assert not verify(env, public_b64(key))


def test_envelope_dict_round_trip():
    key = generate_private_key()
    env = sign({"x": 1}, key, "gate")
    assert Envelope.from_dict(env.to_dict()) == env


def test_key_file_round_trip(tmp_path):
    key = generate_private_key()
    save_private_key(key, tmp_path / "sub" / "k.key")
    assert public_b64(load_private_key(tmp_path / "sub" / "k.key")) == public_b64(key)


def test_key_file_is_rewritten_owner_only(tmp_path):
    key = generate_private_key()
    path = tmp_path / "k.key"
    path.write_text("stale key material that is longer than the new one", encoding="utf-8")
    if os.name != "nt":
        path.chmod(0o644)
    save_private_key(key, path)
    assert public_b64(load_private_key(path)) == public_b64(key)  # an existing file is replaced, not appended to
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600  # and tightened to owner-only, even if it was looser before


def test_signer_is_bound_into_signature():
    key = generate_private_key()
    env = sign({"x": 1}, key, "user")
    relabelled = Envelope(payload=env.payload, signer="attacker", sig=env.sig)
    assert not verify(relabelled, public_b64(key))


def test_json_text_round_trip_with_nested_unicode_payload():
    key = generate_private_key()
    env = sign({"items": [{"sku": "RICE5", "qty": 2}], "note": "chai ☕", "ok": True, "none": None}, key, "merchant:kirana-one")
    wire = json.loads(json.dumps(env.to_dict()))
    assert verify(Envelope.from_dict(wire), public_b64(key))


def test_malleable_signature_encodings_are_rejected():
    key = generate_private_key()
    env = sign({"x": 1}, key, "user")
    pub = public_b64(key)
    for bad in (env.sig + "=", env.sig + "\n", env.sig.replace("_", "/") if "_" in env.sig else env.sig + "+", "!!" + env.sig):
        assert not verify(Envelope(payload=env.payload, signer=env.signer, sig=bad), pub)


def test_wrong_length_public_key_fails_closed():
    key = generate_private_key()
    env = sign({"x": 1}, key, "user")
    assert not verify(env, public_b64(key)[:-4])
    assert not verify(env, "")


def test_canonical_json_rejects_nan():
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})
