"""Tests for the plug-in-triggered immediate replan (Application._check_ev_connected).

Without this, a car plugged in shortly after a planning cycle would sit
without a charging goal for up to plan_interval_s (now 15 min, previously up
to an hour) before the next scheduled cycle picked it up.
"""

from __future__ import annotations

import asyncio

import pytest

from energy_assistant.assets.ev import EvChargingAsset
from energy_assistant.core.models import DeviceState
from energy_assistant.server import Application


def _asset(asset_id: str = "ev1", device_id: str = "wallbox") -> EvChargingAsset:
    return EvChargingAsset(
        asset_id=asset_id,
        device_id=device_id,
        label="EV",
        capacity_kwh=60.0,
        max_charge_kw=11.0,
    )


def _app_with_asset() -> tuple[Application, list[int]]:
    app = Application()
    app._ev_assets = [_asset()]
    app._ev_prev_connected = {}
    calls: list[int] = []

    async def fake_run_plan() -> None:
        calls.append(1)

    app._run_plan = fake_run_plan  # type: ignore[method-assign]
    return app, calls


async def _settle() -> None:
    # Let the asyncio.create_task(...) scheduled inside _check_ev_connected run.
    await asyncio.sleep(0)


async def test_connect_transition_triggers_replan() -> None:
    app, calls = _app_with_asset()

    # First tick: unknown → not connected. No transition, no replan.
    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=False)})
    await _settle()
    assert calls == []

    # Car plugs in: not connected → connected. Should trigger an immediate replan.
    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=True)})
    await _settle()
    assert calls == [1]


async def test_staying_connected_does_not_replan_again() -> None:
    app, calls = _app_with_asset()

    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=False)})
    await _settle()
    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=True)})
    await _settle()
    assert calls == [1]

    # Still connected next tick — no new transition, no extra replan.
    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=True)})
    await _settle()
    assert calls == [1]


async def test_disconnect_does_not_trigger_replan() -> None:
    app, calls = _app_with_asset()

    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=False)})
    await _settle()
    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=True)})
    await _settle()
    assert calls == [1]

    app._check_ev_connected({"wallbox": DeviceState(device_id="wallbox", available=False)})
    await _settle()
    assert calls == [1]
