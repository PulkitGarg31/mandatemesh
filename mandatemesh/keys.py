"""The four signing identities. Private keys never leave this process; the agent gets only its own."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mandatemesh.crypto import generate_private_key, load_private_key, public_b64, save_private_key

ROLES = ("user", "agent", "merchant", "gate")


@dataclass
class Keys:
    user: Ed25519PrivateKey
    agent: Ed25519PrivateKey
    merchant: Ed25519PrivateKey
    gate: Ed25519PrivateKey

    @classmethod
    def generate(cls) -> "Keys":
        return cls(*(generate_private_key() for _ in ROLES))

    def save(self, directory: Path) -> None:
        for role in ROLES:
            save_private_key(getattr(self, role), directory / f"{role}.key")

    @classmethod
    def load(cls, directory: Path) -> "Keys":
        missing = [r for r in ROLES if not (directory / f"{r}.key").exists()]
        if missing:
            raise FileNotFoundError(f"missing key files {missing} in {directory}; run: python -m mandatemesh keys init")
        return cls(*(load_private_key(directory / f"{r}.key") for r in ROLES))

    def pub(self, role: str) -> str:
        return public_b64(getattr(self, role))
