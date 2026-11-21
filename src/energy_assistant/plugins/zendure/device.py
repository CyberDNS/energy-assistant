"""ZendureDevice — Zendure SolarFlow / Hyper battery controlled directly via MQTT.

Replaces the ioBroker ``zendure-solarflow`` adapter path (``zendure_iobroker``)
with the same control semantics, talking to the device's MQTT topics through a
``ZendureBridge`` (Zendure cloud broker or a local broker).

Device properties used (raw MQTT names, see ``bridge.py`` for topics)
--------------------------------------------------------------------
Read:
``electricLevel``    SoC (%)
``outputPackPower``  power INTO the packs (W, charging)
``packInputPower``   power OUT OF the packs (W, discharging)
``solarInputPower`` / ``outputHomePower`` / ``gridInputPower``  (W)
``acMode``           1 = AC input (charge from grid), 2 = AC output
``autoModel``        0 = manual (required for limit writes)
``inputLimit`` / ``outputLimit``  active AC charge / discharge limits (W)
``minSoc`` / ``socSet``  discharge / charge SoC limits in ‰ (value × 10)
``inverseMaxPower``  inverter output cap (W)

Write (``set_power_w``), same sequence as the ioBroker adapter:
charge     → ``autoModel=0``, ``acMode=1``, ``inputLimit=W``, ``outputLimit=0``
discharge  → ``autoModel=0``, ``acMode=2``, ``outputLimit=W``, ``inputLimit=0``
idle       → ``inputLimit=0``, ``outputLimit=0``

Properties whose reported value already matches are not re-sent: limit writes
persist to the device's flash unless ``smartMode=1`` (set ``smart_mode: true``
to enable it once on start-up, which Zendure recommends for frequent control).

Sign convention: ``power_w > 0`` charging, ``< 0`` discharging.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from ...core.change_gate import ChangeGate
from ...core.device_model import DeviceCategory
from ...core.models import DeviceCommand, DeviceState, StorageConstraints
from .bridge import ZendureBridge

_log = logging.getLogger(__name__)

# Power readings not refreshed within this window are treated as 0 (the device
# does not always report a drop to 0) — same rule as the ioBroker adapter.
_POWER_STALE_S = 300.0
_POWER_PROPS = ("outputPackPower", "packInputPower", "outputHomePower",
                "gridInputPower", "solarInputPower")
# The device rejects AC input limits between 1 and 30 W.
_MIN_INPUT_LIMIT_W = 30


class ZendureDevice:
    """Controllable storage ``Device`` for a Zendure battery over MQTT."""

    def __init__(
        self,
        device_id: str,
        bridge: ZendureBridge,
        product_key: str,
        device_key: str,
        *,
        capacity_kwh: float,
        min_soc_pct: float = 0.0,
        max_soc_pct: float = 100.0,
        max_charge_kw: float,
        max_discharge_kw: float,
        maintenance_charge_w: float = 300.0,
        purchase_price_eur: float | None = None,
        cycle_life: int | None = None,
        smart_mode: bool = False,
        stale_after_s: float = 300.0,
    ) -> None:
        self._device_id = device_id
        self._bridge = bridge
        self._pk = product_key
        self._dk = device_key
        self._capacity_kwh = capacity_kwh
        self._min_soc_pct = min_soc_pct
        self._max_soc_pct = max_soc_pct
        self._max_charge_kw = max_charge_kw
        self._max_discharge_kw = max_discharge_kw
        self._maintenance_charge_w = maintenance_charge_w
        self._purchase_price_eur = purchase_price_eur
        self._cycle_life = cycle_life
        self._smart_mode = smart_mode
        self._smart_mode_sent = False
        self._stale_after_s = stale_after_s
        self._log_gate = ChangeGate()
        bridge.register(product_key, device_key)

    # ------------------------------------------------------------------
    # Static description
    # ------------------------------------------------------------------

    @property
    def device_id(self) -> str:
        return self._device_id

    @property
    def category(self) -> DeviceCategory:
        return DeviceCategory.BATTERY

    @property
    def storage_constraints(self) -> StorageConstraints:
        return StorageConstraints(
            device_id=self._device_id,
            capacity_kwh=self._capacity_kwh,
            max_charge_kw=self._max_charge_kw,
            max_discharge_kw=self._max_discharge_kw,
            min_soc_pct=self._min_soc_pct,
            max_soc_pct=self._max_soc_pct,
            purchase_price_eur=self._purchase_price_eur,
            cycle_life=self._cycle_life,
        )

    @property
    def maintenance_charge_w(self) -> float:
        return self._maintenance_charge_w

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def _prop(self, name: str, now: float) -> float | None:
        entry = self._bridge.cache(self._pk, self._dk).properties.get(name)
        if entry is None:
            return None
        value, ts = entry
        if name in _POWER_PROPS and now - ts > _POWER_STALE_S:
            return 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    async def get_state(self) -> DeviceState:
        """Return the cached report state.

        Unavailable while the bridge is disconnected, before the first report,
        or when the device has been silent for ``stale_after_s``.
        """
        self._bridge.ensure_started()
        now = time.monotonic()
        cache = self._bridge.cache(self._pk, self._dk)
        if (
            not self._bridge.connected
            or cache.last_message is None
            or now - cache.last_message > self._stale_after_s
            # The first full report arrives in several partial messages —
            # don't report a state before the SoC is known.
            or "electricLevel" not in cache.properties
        ):
            return DeviceState(device_id=self._device_id, available=False)

        charge_w = self._prop("outputPackPower", now) or 0.0
        disch_w = self._prop("packInputPower", now) or 0.0
        min_soc = self._prop("minSoc", now)
        max_soc = self._prop("socSet", now)

        return DeviceState(
            device_id=self._device_id,
            timestamp=datetime.now(timezone.utc),
            power_w=charge_w - disch_w,
            soc_pct=self._prop("electricLevel", now),
            available=True,
            extra={
                "charge_w": charge_w,
                "discharge_w": disch_w,
                "home_w": self._prop("outputHomePower", now),
                "grid_input_w": self._prop("gridInputPower", now),
                "solar_w": self._prop("solarInputPower", now),
                "ac_mode": self._prop("acMode", now),
                "auto_model": self._prop("autoModel", now),
                "input_limit_w": self._prop("inputLimit", now),
                "output_limit_w": self._prop("outputLimit", now),
                "min_soc_pct": min_soc / 10 if min_soc is not None else None,
                "max_soc_pct": max_soc / 10 if max_soc is not None else None,
                "pack_count": len(cache.packs) or self._prop("packNum", now),
            },
        )

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    async def send_command(self, command: DeviceCommand) -> None:
        """Supported: ``set_power_w`` (W, + charge / − discharge / 0 idle),
        ``set_charge_limit`` and ``set_discharge_limit`` (SoC %)."""
        self._bridge.ensure_started()

        if command.command == "set_power_w":
            await self._set_power(float(command.value))
        elif command.command == "set_charge_limit":
            pct = int(round(float(command.value)))
            if not 40 <= pct <= 100:
                raise ValueError(f"Zendure charge limit must be 40–100 %, got {pct}")
            await self._write([("socSet", pct * 10)])
        elif command.command == "set_discharge_limit":
            pct = int(round(float(command.value)))
            if not 0 <= pct <= 50:
                raise ValueError(f"Zendure discharge limit must be 0–50 %, got {pct}")
            await self._write([("minSoc", pct * 10)])
        else:
            raise NotImplementedError(
                f"ZendureDevice does not support command {command.command!r}"
            )

    async def _set_power(self, power_w: float) -> None:
        writes: list[tuple[str, int]] = []
        if self._smart_mode and not self._smart_mode_sent:
            writes.append(("smartMode", 1))

        if power_w > 0:
            limit = min(int(round(power_w)), int(self._max_charge_kw * 1000))
            limit = max(limit, _MIN_INPUT_LIMIT_W)
            writes += [("autoModel", 0), ("acMode", 1), ("inputLimit", limit), ("outputLimit", 0)]
            desc = f"charge {limit} W (acMode=1, inputLimit={limit})"
        elif power_w < 0:
            limit = min(int(round(-power_w)), int(self._max_discharge_kw * 1000))
            writes += [("autoModel", 0), ("acMode", 2), ("outputLimit", limit), ("inputLimit", 0)]
            desc = f"discharge {limit} W (acMode=2, outputLimit={limit})"
        else:
            writes += [("inputLimit", 0), ("outputLimit", 0)]
            desc = "idle (both limits cleared)"

        sent = await self._write(writes)
        if any(name == "smartMode" for name in sent):
            self._smart_mode_sent = True
        if self._log_gate.changed("power", desc):
            _log.info("ZendureDevice %r: %s", self._device_id, desc)

    async def _write(self, writes: list[tuple[str, int]]) -> list[str]:
        """Send each property whose reported value differs, in order.

        One message per property, mirroring the ioBroker adapter (the device
        evaluates ``acMode`` before the limits).  Returns the names sent.
        """
        now = time.monotonic()
        sent: list[str] = []
        for name, value in writes:
            if name != "smartMode" and self._prop(name, now) == value:
                continue
            await self._bridge.write_properties(self._pk, self._dk, {name: value})
            sent.append(name)
        return sent
