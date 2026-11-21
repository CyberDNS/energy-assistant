"""Residual (learned) household baseline, default learning for consumers and
the import of the Home Assistant Energy dashboard (issue #65)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from helpers.fake_ha_client import FakeHAClient

from energy_assistant.core.config import AppConfig, BackendsConfig
from energy_assistant.core.device_model import Capability, DeviceCategory, parse_devices
from energy_assistant.core.plugin_registry import BuildContext
from energy_assistant.loader.device_loader import build_device_forecast_map
from energy_assistant.loader.energy_dashboard import (
    CACHE_FILE,
    add_dashboard_consumers,
    import_energy_dashboard,
)
from energy_assistant.loader.forecast_config import (
    default_learning_cfg,
    effective_forecast_configs,
)
from energy_assistant.plugins.learned.forecast import (
    LearnedConsumptionForecast,
    hourly_watts,
    residual_watts,
)

NOW = datetime(2026, 11, 15, 10, 30, tzinfo=UTC)
HOUSE = "sensor.power_consumption_household"
RACK = "sensor.power_consumption_it_rack"
FREEZER_KWH = "sensor.energy_consumption_freezer"
T0 = datetime(2026, 11, 1, tzinfo=UTC)

STATIC = {
    "type": "static_profile",
    "profile": {"weekdays": [{"hour": 0, "consumed_kwh": 24.0}]},
}


def _h(n: int) -> datetime:
    return T0 + timedelta(hours=n)


# ---------------------------------------------------------------------------
# residual arithmetic
# ---------------------------------------------------------------------------


def test_hourly_watts_reads_mean_or_energy_change():
    rows = [
        {"start": _h(0), "mean": 250.0},
        {"start": _h(1), "change": 120.0},  # Wh in one hour == 120 W
        {"start": _h(2), "mean": None, "change": None},
    ]
    assert hourly_watts(rows) == {_h(0): 250.0, _h(1): 120.0}
    assert hourly_watts(rows, scale=1000.0)[_h(0)] == 250_000.0


def test_residual_subtracts_parts_and_flags_gaps():
    target = {_h(i): 1000.0 for i in range(6)}
    parts = {
        # metered from hour 2 on: before that it is part of the residual
        "late": {_h(2): 100.0, _h(3): 100.0, _h(4): 100.0, _h(5): 100.0},
        # missing hour 3 while drawing power: a meter outage
        "gappy": {_h(0): 200.0, _h(1): 200.0, _h(2): 200.0, _h(4): 1200.0, _h(5): 0.0},
        # silent after reading idle: switched off, counts as zero
        "off": {_h(0): 1.0},
    }
    residual, gaps = residual_watts(target, parts)
    assert residual == {
        _h(0): 799.0, _h(1): 800.0, _h(2): 700.0, _h(4): 0.0, _h(5): 900.0,
    }
    assert gaps == {_h(3)}


# ---------------------------------------------------------------------------
# learned forecast with subtract
# ---------------------------------------------------------------------------


def _residual_ha(days: int = 30) -> FakeHAClient:
    """House = 500 W baseline + 300 W rack + 50 W freezer (energy meter)."""
    ha = FakeHAClient()
    house, rack, freezer = [], [], []
    today = NOW.astimezone().date()
    for i in range(days):
        day = today - timedelta(days=days - i)
        for h in range(24):
            ts = datetime(day.year, day.month, day.day, h).astimezone(UTC)
            house.append({"start": ts, "mean": 850.0, "min": 800.0, "max": 900.0})
            rack.append({"start": ts, "mean": 300.0, "min": 300.0, "max": 300.0})
            freezer.append({"start": ts, "change": 50.0})
    ha.statistics = {HOUSE: house, RACK: rack, FREEZER_KWH: freezer}
    return ha


async def test_learned_residual_excludes_subtracted_consumers():
    ha = _residual_ha()
    provider = LearnedConsumptionForecast(
        "baseline_forecast", ha, HOUSE, subtract=[RACK, FREEZER_KWH, HOUSE],
        clock=lambda: NOW,
    )
    points = await provider.get_forecast(timedelta(hours=3))
    assert provider.model is not None
    assert provider.model.level_kwh["all"] == pytest.approx(0.5 * 24)
    assert points[0].value == pytest.approx(0.5, rel=0.01)
    # HA converts power to W and energy to Wh; the target itself is not
    # subtracted from itself.
    assert ha.last_units == {"power": "W", "energy": "Wh"}
    assert provider.subtract == [RACK, FREEZER_KWH]
    diag = await provider.diagnostics(timedelta(hours=3))
    assert diag["subtract"] == [RACK, FREEZER_KWH]


async def test_explicit_history_unit_reads_raw_means():
    ha = _residual_ha()
    provider = LearnedConsumptionForecast(
        "rack_forecast", ha, RACK, history_unit="W", clock=lambda: NOW
    )
    await provider.get_forecast(timedelta(hours=1))
    assert ha.last_units is None
    assert provider.model is not None


async def test_meter_gap_drops_the_day_from_training():
    ha = _residual_ha()
    # The rack meter misses one hour on the oldest day only.
    gone = ha.statistics[RACK][5]["start"]
    ha.statistics[RACK] = [r for r in ha.statistics[RACK] if r["start"] != gone]
    provider = LearnedConsumptionForecast(
        "baseline_forecast", ha, HOUSE, subtract=[RACK], clock=lambda: NOW
    )
    days = await provider.fetch_days()
    assert len(days) == 29
    assert gone.astimezone().date() not in {d.day for d in days}


# ---------------------------------------------------------------------------
# default learning + subtract: [auto]
# ---------------------------------------------------------------------------


def _ha_consumer(entity: str, **extra) -> dict:
    return {"category": "consumer", "type": "generic_homeassistant",
            "capabilities": {"power_w": {"entity": entity}}, **extra}


def _spec(raw: dict):
    """One device entry parsed into a validated DeviceSpec."""
    return parse_devices({"d": raw})["d"]


def test_basic_consumer_learns_by_default():
    assert default_learning_cfg(_spec(_ha_consumer(RACK))) == {
        "type": "learned", "history_entity": RACK,
    }


def test_static_profile_becomes_the_fallback():
    fc = default_learning_cfg(_spec(_ha_consumer(RACK, forecast=STATIC)))
    assert fc == {"type": "learned", "history_entity": RACK, "fallback": STATIC}


def test_learning_options_and_opt_out():
    fc = default_learning_cfg(_spec(_ha_consumer(RACK, learning={"history_days": 90})))
    assert fc["history_days"] == 90
    assert default_learning_cfg(_spec(_ha_consumer(RACK, learning=False))) is None
    assert default_learning_cfg(_spec(_ha_consumer(RACK, learning=False, forecast=STATIC))) == STATIC


def test_default_learning_skips_meters_virtual_and_explicit_forecasts():
    assert default_learning_cfg(_spec({**_ha_consumer(RACK), "category": "meter"})) is None
    assert default_learning_cfg(_spec({"category": "consumer", "type": "generic_consumer"})) is None
    explicit = {"type": "learned", "history_entity": "sensor.other"}
    assert default_learning_cfg(_spec(_ha_consumer(RACK, forecast=explicit))) == explicit


def test_auto_subtract_takes_learned_consumers_on_the_same_tariff():
    app = AppConfig(
        default_tariff_id="household",
        devices=parse_devices({
            "baseline": {
                "category": "consumer", "type": "generic_consumer",
                "forecast": {"type": "learned", "history_entity": HOUSE,
                             "subtract": ["auto", "sensor.extra"]},
            },
            "it_rack": _ha_consumer(RACK),
            "freezer": {"category": "consumer", "type": "generic_consumer",
                        "forecast": {"type": "learned", "history_entity": FREEZER_KWH}},
            # other tariff (separate meter): not inside the household sensor
            "heatpump": _ha_consumer("sensor.hp", tariff="heatpump"),
            # inside another meter: subtracted as part of that one
            "lamp": _ha_consumer("sensor.lamp", included_in="it_rack"),
            "opted_out": _ha_consumer("sensor.tv", learning=False),
            "static_only": {"category": "consumer", "type": "generic_consumer", "forecast": STATIC},
            "grid": {"category": "grid_meter", "type": "generic_homeassistant",
                     "capabilities": {"power_w": {"entity": "sensor.g"}}},
        }),
    )
    fcs = effective_forecast_configs(app)
    assert fcs["baseline"]["subtract"] == [RACK, FREEZER_KWH, "sensor.extra"]
    assert set(fcs) == {"baseline", "it_rack", "freezer", "heatpump", "lamp", "static_only"}

    providers = build_device_forecast_map(
        app, BuildContext(backends=BackendsConfig(), ha_client=FakeHAClient())
    )
    assert providers["baseline"].subtract == [RACK, FREEZER_KWH, "sensor.extra"]
    assert isinstance(providers["it_rack"], LearnedConsumptionForecast)


# ---------------------------------------------------------------------------
# Energy dashboard import
# ---------------------------------------------------------------------------

PREFS = {
    "energy_sources": [],
    "device_consumption": [
        {"stat_consumption": "sensor.it_rack_energy", "name": "IT Rack",
         "stat_rate": RACK},
        {"stat_consumption": FREEZER_KWH, "name": "Freezer"},
        {"stat_consumption": "sensor.heat_pump_energy", "name": "Heatpump",
         "stat_rate": "sensor.power_consumption_heatpump"},
        {"stat_consumption": "sensor.chargepoint_bella_energy", "name": "Scooter",
         "stat_rate": "sensor.power_consumption_car_laurence"},
        {"stat_consumption": "sensor.nas_energy", "name": "NAS",
         "stat_rate": "sensor.nas_power", "included_in_stat": "sensor.it_rack_energy"},
    ],
}


def _app() -> AppConfig:
    return AppConfig(
        default_tariff_id="household",
        devices=parse_devices({
            "heatpump_meter": {
                "category": "consumer", "type": "differential", "tariff": "heatpump",
                "forecast": {"type": "learned",
                             "history_entity": "sensor.power_consumption_heatpump"},
            },
            "baseline": {
                "category": "consumer", "type": "generic_consumer",
                "forecast": {"type": "learned", "history_entity": HOUSE,
                             "subtract": ["auto"], "fallback": STATIC},
            },
        }),
    )


def test_dashboard_consumers_are_added_with_learning():
    app = _app()
    added = add_dashboard_consumers(
        app, PREFS, {"import_consumers": True, "exclude": ["Scooter"],
                     "learning": {"history_days": 120}},
    )
    # heat pump already configured, the car charger excluded
    assert added == ["it_rack", "freezer", "nas"]
    rack = app.devices["it_rack"]
    assert rack.type == "generic_homeassistant"
    assert rack.bound_entity(Capability.POWER_W) == RACK
    assert rack.category is DeviceCategory.CONSUMER
    assert rack.label == "IT Rack"
    freezer = app.devices["freezer"]
    assert freezer.type == "generic_consumer"
    assert freezer.forecast == {
        "type": "learned", "history_entity": FREEZER_KWH, "history_days": 120,
    }
    assert app.devices["nas"].included_in == "it_rack"

    fcs = effective_forecast_configs(app)
    # the rack learns its own load minus the NAS inside it …
    assert fcs["it_rack"] == {
        "type": "learned", "history_entity": RACK, "history_days": 120,
        "subtract": ["sensor.nas_power"],
    }
    assert fcs["nas"]["history_entity"] == "sensor.nas_power"
    # … and the baseline subtracts the rack (incl. NAS) and the freezer,
    # not the heat pump on its own tariff.
    assert fcs["baseline"]["subtract"] == [RACK, FREEZER_KWH]


def test_dashboard_exclude_matches_entities_and_ids():
    app = _app()
    added = add_dashboard_consumers(app, PREFS, {
        "exclude": ["sensor.it_rack_energy", "sensor.power_consumption_car_laurence", "nas"],
    })
    assert added == ["freezer"]


def test_dashboard_without_learning_imports_live_sensors_only():
    app = _app()
    added = add_dashboard_consumers(app, PREFS, {"learning": False, "exclude": ["Scooter"]})
    assert added == ["it_rack", "nas"]
    assert "it_rack" not in effective_forecast_configs(app)


def test_dashboard_device_id_does_not_clash():
    app = _app()
    app.devices["freezer"] = _spec({"category": "consumer", "type": "generic_consumer"})
    added = add_dashboard_consumers(app, {"device_consumption": [PREFS["device_consumption"][1]]}, {})
    assert added == ["freezer_2"]


async def test_import_is_opt_in(tmp_path):
    ha = FakeHAClient()
    ha.energy_prefs = PREFS
    app = _app()
    assert await import_energy_dashboard(app, ha, tmp_path) == []
    assert not (tmp_path / CACHE_FILE).exists()


async def test_import_caches_and_falls_back_to_the_cache(tmp_path):
    ha = FakeHAClient()
    ha.energy_prefs = PREFS
    app = _app()
    app.energy_dashboard = {"import_consumers": True, "exclude": ["Scooter"]}
    assert await import_energy_dashboard(app, ha, tmp_path) == ["it_rack", "freezer", "nas"]
    assert json.loads((tmp_path / CACHE_FILE).read_text()) == PREFS

    # HA down at the next start: the cached copy keeps the same devices.
    ha.energy_prefs = None
    app2 = _app()
    app2.energy_dashboard = app.energy_dashboard
    assert await import_energy_dashboard(app2, ha, tmp_path) == ["it_rack", "freezer", "nas"]

    # No cache either: nothing imported, no error.
    app3 = _app()
    app3.energy_dashboard = app.energy_dashboard
    assert await import_energy_dashboard(app3, ha, tmp_path / "empty") == []
    assert await import_energy_dashboard(app3, None, tmp_path) == []
