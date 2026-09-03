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
