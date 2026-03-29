from __future__ import annotations

import pytest

from energy_assistant.core.models import DeviceRole
from energy_assistant.plugins.generic_homeassistant.device import GenericHADevice
from helpers.fake_ha_client import FakeHAClient


@pytest.mark.asyncio
async def test_generic_ha_device_invert_sign_single_entity() -> None:
    client = FakeHAClient({"sensor.power_production_pv": 2036.0})
    dev = GenericHADevice(
        device_id="pv_production",
        role=DeviceRole.PRODUCER,
        client=client,
        entity_power="sensor.power_production_pv",
        invert_sign=True,
    )

    state = await dev.get_state()

    assert state.available is True
    assert state.power_w == -2036.0
