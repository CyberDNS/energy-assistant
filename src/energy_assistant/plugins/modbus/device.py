"""ModbusDevice — a device read directly over Modbus TCP, driven by a profile.

The device itself knows nothing about any vendor: which registers to read and
how they map onto ``DeviceState`` comes entirely from a ``ModbusProfile``
(see ``profile.py``).

Reading
-------
The registers the profile maps to state are grouped into as few read requests
as possible once, at construction.  Each ``get_state()`` issues those
requests:

* a register whose raw value equals the profile's "no value" sentinel, or
  whose request the device rejected, yields ``None`` for that field,
* the device is reported ``available=False`` only when *no* request succeeded
  (connection down, wrong unit id, …).

Control
-------
Vendor control logic differs too much to be a register mapping, so the
profile selects a named strategy (see ``control.py``).  The strategy turns a
``DeviceCommand`` into register values; this class encodes and writes them.

Every command is written, even when the value is unchanged — the control loop
re-sends its setpoint each tick, which also serves as a refresh for inverters
that fall back to a default when commands stop.

Without a strategy the device is read-only and ignores commands (the
``Device`` protocol allows that).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from ...core.change_gate import ChangeGate
from ...core.models import DeviceCommand, DeviceRole, DeviceState
from .._modbus import codec
from .._modbus.client import ModbusClientProtocol, ModbusError
from .control import ControlStrategy
from .profile import ModbusProfile, RegisterDef, StateSource

_log = logging.getLogger(__name__)


class ModbusDevice:
    """Profile-driven Modbus TCP device.

    Parameters
    ----------
    device_id:
        Stable unique identifier (e.g. ``"sma_battery"``).
    client:
        Modbus transport, normally from ``ModbusConnectionPool``.
    profile:
        Register mapping of the device model.
    role:
        What the device is in the energy system (from ``role:`` in config).
    unit_id:
        Modbus unit / slave id; defaults to the profile's.
    strategy:
        Control strategy, or ``None`` for a read-only device.
    """

    def __init__(
        self,
        device_id: str,
        client: ModbusClientProtocol,
        profile: ModbusProfile,
        *,
        role: DeviceRole = DeviceRole.STORAGE,
        unit_id: int | None = None,
        strategy: ControlStrategy | None = None,
    ) -> None:
        self._device_id = device_id
        self._client = client
        self._profile = profile
        self._role = role
        self._unit_id = profile.unit_id if unit_id is None else unit_id
        self._strategy = strategy
        self._polled = profile.polled_registers()
        self._blocks = codec.plan_reads((r.table, r.address, r.count) for r in self._polled)
        self._log_gate = ChangeGate()

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def role(self) -> DeviceRole:
        return self._role

    @property
    def profile(self) -> ModbusProfile:
        return self._profile

    @property
    def read_only(self) -> bool:
        return self._strategy is None

    @property
    def controls_charge(self) -> bool:
        """Whether a charge setpoint actually reaches the hardware.

        Read by callers that describe what a command will really do (e.g.
        dry-run logging).
        """
        return self._strategy is not None and self._strategy.controls_charge

    async def read_registers(self) -> tuple[dict[str, float | None], list[str]]:
        """Read every polled register once.

        Returns ``(values, errors)``: decoded and scaled values by register
        name (``None`` = no value), and one message per failed read request.
        """
        values: dict[str, float | None] = {r.name: None for r in self._polled}
        errors: list[str] = []
        for block in self._blocks:
            try:
                words = await self._client.read(
                    block.table, block.address, block.count, self._unit_id
                )
            except ModbusError as exc:
                errors.append(str(exc))
                continue
            for reg in self._polled:
                if block.covers(reg.table, reg.address, reg.count):
                    values[reg.name] = self._decode(reg, block.slice(reg.address, reg.count, words))
        return values, errors

    def _decode(self, reg: RegisterDef, words: list[int]) -> float | None:
        if len(words) != reg.count:
            return None
        order = self._profile.word_order
        sentinel = self._profile.nan.get(reg.type)
        if sentinel is not None and codec.raw_unsigned(words, order) == sentinel:
            return None
        return float(codec.decode(words, reg.type, order)) * reg.scale

    async def get_state(self) -> DeviceState:
        values, errors = await self.read_registers()

        if errors and len(errors) == len(self._blocks):
            if self._log_gate.changed("available", False):
                _log.warning(
                    "ModbusDevice %r: unreachable — %s", self._device_id, errors[0]
                )
            return DeviceState(device_id=self._device_id, available=False)

        if self._log_gate.changed("available", True):
            _log.info("ModbusDevice %r: connected (%s)", self._device_id, self._profile.name)
        if self._log_gate.changed("errors", tuple(errors)) and errors:
            _log.warning(
                "ModbusDevice %r: %d read(s) failed — %s",
                self._device_id, len(errors), "; ".join(errors),
            )

        def pick(source: StateSource | None) -> float | None:
            if source is None:
                return None
            value = values.get(source.register)
            return None if value is None else value * source.scale

        state = self._profile.state
        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=pick(state.get("power_w")),
            soc_pct=pick(state.get("soc_pct")),
            energy_kwh=pick(state.get("energy_kwh")),
            available=True,
            extra={key: pick(source) for key, source in self._profile.extra.items()},
        )

    async def send_command(self, command: DeviceCommand) -> None:
        """Translate *command* via the profile's strategy and write the result.

        Raises ``NotImplementedError`` for commands the strategy does not
        support and ``ModbusError`` when a write fails.  A read-only device
        ignores the command.
        """
        if self._strategy is None:
            if self._log_gate.changed("command_ignored", True):
                _log.warning(
                    "ModbusDevice %r: read-only, ignoring command %r (and further ones)",
                    self._device_id, command.command,
                )
            return

        writes = self._strategy.plan(command)
        for name, value in writes:
            reg = self._profile.registers[name]
            raw = value / reg.scale
            if not reg.type.startswith("f"):
                raw = int(round(raw))
            words = codec.encode(raw, reg.type, self._profile.word_order)
            await self._client.write(reg.address, words, self._unit_id)

        summary = tuple(writes)
        if self._log_gate.changed("writes", summary):
            # A non-zero write is a genuine setpoint; all-zero covers both
            # "idle" and requests the hardware cannot act on.
            level = logging.INFO if any(value for _, value in writes) else logging.DEBUG
            _log.log(
                level,
                "ModbusDevice %r: %s (requested %s = %s)",
                self._device_id,
                ", ".join(f"{name} = {value:g}" for name, value in writes),
                command.command,
                command.value,
            )
