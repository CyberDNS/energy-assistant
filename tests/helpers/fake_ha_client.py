"""Fake Home Assistant client for unit tests."""

from __future__ import annotations

from typing import Any


class FakeHAClient:
    """In-memory HAClient stub that implements HAClientProtocol."""

    def __init__(self, states: dict[str, Any] | None = None) -> None:
        self._states: dict[str, Any] = dict(states or {})
        # Records every call_service invocation as (domain, service, data).
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get_entity_state(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    async def call_service(
        self,
        domain: str,
        service: str,
        data: dict[str, Any] | None = None,
    ) -> None:
        self.calls.append((domain, service, data or {}))
