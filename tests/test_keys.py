import pytest

from mandatemesh.crypto import public_b64
from mandatemesh.keys import ROLES, Keys


def test_generate_has_five_distinct_roles():
    k = Keys.generate()
    pubs = {k.pub(r) for r in ROLES}
    assert ROLES == ("user", "agent", "merchant", "gate", "planner")
    assert len(pubs) == 5


def test_save_and_load_round_trip(tmp_path):
    k = Keys.generate()
    k.save(tmp_path / "keys")
    loaded = Keys.load(tmp_path / "keys")
    for role in ROLES:
        assert loaded.pub(role) == k.pub(role)
        assert public_b64(getattr(loaded, role)) == k.pub(role)


def test_load_missing_raises_with_hint(tmp_path):
    with pytest.raises(FileNotFoundError, match="keys init"):
        Keys.load(tmp_path / "nope")
