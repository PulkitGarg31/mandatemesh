"""UAP-style trusted-agent registry in miniature: register, look up identity, revoke."""
from __future__ import annotations

from dataclasses import dataclass

ACTIVE = "active"
REVOKED = "revoked"


@dataclass
class AgentRecord:
    agent_id: str
    pubkey_b64: str
    status: str = ACTIVE


class AgentRegistry:
    def __init__(self) -> None:
        self._agents: dict[str, AgentRecord] = {}

    def register(self, agent_id: str, pubkey_b64: str) -> AgentRecord:
        existing = self._agents.get(agent_id)
        if existing is not None and existing.status == REVOKED:
            raise ValueError(f"agent '{agent_id}' is revoked; revocation is permanent in this registry")
        rec = AgentRecord(agent_id=agent_id, pubkey_b64=pubkey_b64)
        self._agents[agent_id] = rec
        return rec

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._agents.get(agent_id)

    def is_active(self, agent_id: str) -> bool:
        rec = self.get(agent_id)
        return rec is not None and rec.status == ACTIVE

    def revoke(self, agent_id: str) -> None:
        self._agents[agent_id].status = REVOKED
