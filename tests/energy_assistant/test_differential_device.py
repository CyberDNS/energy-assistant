"""Tests for DifferentialDevice (Messkonzept 8 core logic)."""

from __future__ import annotations

import pytest

from energy_assistant.core.models import DeviceCommand, DeviceRole, DeviceState
from energy_assistant.plugins.differential.device import DifferentialDevice


# ---------------------------------------------------------------------------
# Fake Device stub
# ---------------------------------------------------------------------------


class _FakeDevice:
    """Minimal Device stub that returns a pre-set DeviceState."""

    def __init__(
        self,
        device_id: str,
        power_w: float | None,
        available: bool = True,
        extra: dict | None = None,
    ) -> None:
        self._device_id = device_id
        self._state = DeviceState(
            device_id=device_id,
            power_w=power_w,
            available=available,
            extra=extra or {},
        )

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return DeviceRole.METER

    async def get_state(self) -> DeviceState:
        return self._state

    async def send_command(self, command: DeviceCommand) -> None:
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDifferentialDevice:
    async def test_basic_subtraction(self) -> None:
        """Main use case: heatpump = main_grid_import - household_import."""
        minuend = _FakeDevice("main_grid_meter", power_w=5000.0)
        subtrahend = _FakeDevice("household_meter", power_w=3000.0)

        hp = DifferentialDevice(
            device_id="heatpump",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
        )
        state = await hp.get_state()

        assert state.device_id == "heatpump"
        assert state.power_w == pytest.approx(2000.0)
        assert state.available is True

    async def test_min_power_clamp(self) -> None:
        """min_power_w = 0.0 prevents negative results from transient errors."""
        minuend = _FakeDevice("m1", power_w=3000.0)
        subtrahend = _FakeDevice("m2", power_w=3050.0)  # slightly higher → negative diff

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
            min_power_w=0.0,
        )
        state = await hp.get_state()
        assert state.power_w == pytest.approx(0.0)

    async def test_max_power_clamp(self) -> None:
        minuend = _FakeDevice("m1", power_w=10000.0)
        subtrahend = _FakeDevice("m2", power_w=0.0)

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
            max_power_w=5000.0,
        )
        state = await hp.get_state()
        assert state.power_w == pytest.approx(5000.0)

    async def test_none_when_minuend_unavailable(self) -> None:
        minuend = _FakeDevice("m1", power_w=None)
        subtrahend = _FakeDevice("m2", power_w=3000.0)

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
        )
        state = await hp.get_state()
        assert state.power_w is None

    async def test_none_when_subtrahend_unavailable(self) -> None:
        minuend = _FakeDevice("m1", power_w=5000.0)
        subtrahend = _FakeDevice("m2", power_w=None)

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
        )
        state = await hp.get_state()
        assert state.power_w is None

    async def test_available_false_when_either_unavailable(self) -> None:
        minuend = _FakeDevice("m1", power_w=5000.0, available=False)
        subtrahend = _FakeDevice("m2", power_w=3000.0, available=True)

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
        )
        state = await hp.get_state()
        assert state.available is False

    async def test_extra_import_w_field(self) -> None:
        """Messkonzept 8: use extra['import_w'] from a bidirectional meter.

        When the main grid meter exposes gross import in extra["import_w"],
        using minuend_field='extra.import_w' gives the correct heat pump
        consumption even when PV is exporting (net power_w would be negative).
        """
        # Simulate: PV exporting 2000 W, household consuming 3000 W,
        # heat pump consuming 0 W.  Net at grid = 3000 - 2000 = +1000 W import.
        # import_w = 3000, export_w = 2000.
        minuend = _FakeDevice(
            "main_grid_meter",
            power_w=1000.0,   # net: import - export
            extra={"import_w": 3000.0, "export_w": 2000.0},
        )
        subtrahend = _FakeDevice("household_meter", power_w=3000.0)

        hp = DifferentialDevice(
            device_id="heatpump",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
            minuend_field="extra.import_w",  # use gross import, not net
            min_power_w=0.0,
        )
        state = await hp.get_state()
        # import_w(3000) - household(3000) = 0 → heat pump is off
        assert state.power_w == pytest.approx(0.0)

    async def test_extra_import_w_with_heatpump_running(self) -> None:
        """Verify correct derivation when both household and heat pump are running."""
        # PV producing 1000 W, household 2000 W, heat pump 3000 W.
        # Grid import = 2000 + 3000 - 1000 = 4000 W.
        # import_w = 4000, export_w = 0.
        minuend = _FakeDevice(
            "main_grid_meter",
            power_w=4000.0,
            extra={"import_w": 4000.0, "export_w": 0.0},
        )
        subtrahend = _FakeDevice("household_meter", power_w=2000.0)

        hp = DifferentialDevice(
            device_id="heatpump",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
            minuend_field="extra.import_w",
            min_power_w=0.0,
        )
        state = await hp.get_state()
        assert state.power_w == pytest.approx(2000.0)

    async def test_extra_field_missing_returns_none(self) -> None:
        minuend = _FakeDevice("m1", power_w=5000.0, extra={})  # no import_w
        subtrahend = _FakeDevice("m2", power_w=3000.0)

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
            minuend_field="extra.import_w",
        )
        state = await hp.get_state()
        assert state.power_w is None

    async def test_send_command_is_noop(self) -> None:
        minuend = _FakeDevice("m1", power_w=5000.0)
        subtrahend = _FakeDevice("m2", power_w=3000.0)

        hp = DifferentialDevice(
            device_id="hp",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
        )
        # Must not raise
        await hp.send_command(DeviceCommand(device_id="hp", command="turn_on"))

    async def test_role_and_device_id_properties(self) -> None:
        minuend = _FakeDevice("m1", power_w=0.0)
        subtrahend = _FakeDevice("m2", power_w=0.0)

        hp = DifferentialDevice(
            device_id="heatpump",
            role=DeviceRole.CONSUMER,
            minuend=minuend,
            subtrahend=subtrahend,
        )
        assert hp.device_id == "heatpump"
        assert hp.role == DeviceRole.CONSUMER
