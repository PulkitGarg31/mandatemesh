"""Ed25519 signing envelopes over canonical JSON. The only module that touches key material."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ALG = "Ed25519"


def canonical_json(payload: dict) -> bytes:
    """Deterministic bytes for signing/hashing: sorted keys, no whitespace, ASCII only."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


_B64U_ALPHABET = re.compile(r"[A-Za-z0-9_-]*")


def unb64u(text: str) -> bytes:
    """Strict unpadded base64url: any other character, padding, or an impossible length is a ValueError."""
    if not isinstance(text, str) or not _B64U_ALPHABET.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not unpadded base64url")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


@dataclass(frozen=True)
class Envelope:
    """A signed payload. JWS-like, deliberately not full JWS. The signature covers alg, signer and payload."""

    payload: dict
    signer: str
    sig: str
    alg: str = ALG

    def to_dict(self) -> dict:
        return {"payload": self.payload, "signer": self.signer, "alg": self.alg, "sig": self.sig}

    @classmethod
    def from_dict(cls, data: dict) -> "Envelope":
        return cls(payload=data["payload"], signer=data["signer"], sig=data["sig"], alg=data.get("alg", ALG))


def generate_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def public_b64(key: Ed25519PrivateKey | Ed25519PublicKey) -> str:
    pub = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return b64u(pub.public_bytes_raw())


def public_from_b64(text: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(unb64u(text))


def signing_input(payload: dict, signer: str, alg: str = ALG) -> bytes:
    """The bytes that are signed: alg and signer are bound in, so neither can be swapped after signing."""
    return canonical_json({"alg": alg, "payload": payload, "signer": signer})


def sign(payload: dict, key: Ed25519PrivateKey, signer: str) -> Envelope:
    return Envelope(payload=payload, signer=signer, sig=b64u(key.sign(signing_input(payload, signer))))


def verify(env: Envelope, pub_b64: str) -> bool:
    """True only if the signature verifies. Any decoding problem is a verification failure."""
    if env.alg != ALG:
        return False
    try:
        public_from_b64(pub_b64).verify(unb64u(env.sig), signing_input(env.payload, env.signer, env.alg))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def save_private_key(key: Ed25519PrivateKey, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(b64u(key.private_bytes_raw()), encoding="utf-8")


def load_private_key(path: Path) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(unb64u(path.read_text(encoding="utf-8").strip()))
