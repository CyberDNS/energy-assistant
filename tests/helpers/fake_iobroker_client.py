"""
Shared fake ioBroker client for tests.

Import ``FakeIoBrokerClient`` in any test module that needs to exercise
ioBroker device or tariff behaviour without real HTTP calls.
"""

from __future__ import annotations

from typing import Any


class FakeIoBrokerClient:
    """
    In-memory ``IoBrokerClientProtocol`` implementation for unit tests.

    ``values`` is the initial state store (object_id → value).
    ``written`` accumulates every ``(object_id, value)`` pair passed to
    ``set_value``, in call order.  Tests can inspect this to verify that
    the correct object IDs were written with the expected values.
    """

    def __init__(self, values: dict[str, Any] | None = None) -> None:
        self._store: dict[str, Any] = dict(values or {})
        self.written: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "FakeIoBrokerClient":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass

    async def get_value(self, object_id: str) -> Any:
        return self._store.get(object_id)

    async def get_bulk(self, object_ids: list[str]) -> dict[str, Any]:
        return {oid: self._store.get(oid) for oid in object_ids}

    async def set_value(self, object_id: str, value: Any) -> None:
        self._store[object_id] = value
        self.written.append((object_id, value))
