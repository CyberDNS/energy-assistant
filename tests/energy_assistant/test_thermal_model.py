"""Tests for the learned thermal model (core/thermal_model.py), its learner
(plugins/learned/thermal.py) and its use in the MILP planner."""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from energy_assistant.core.device_model import parse_devices
from energy_assistant.core.models import (
    DeviceState,
    ForecastPoint,
    ForecastQuantity,
    ThresholdConstraints,
)
from energy_assistant.core.optimizer import OptimizationContext
from energy_assistant.core.thermal_model import (
    COOL,
    HEAT,
    StepSeries,
    ThermalModel,
    Window,
    build_windows,
    fit,
    simulate,
)
from energy_assistant.plugins.learned.thermal import (
    ThermalLearner,
    build_thermal_learners,
)
from energy_assistant.plugins.milp_highs import MilpHigsOptimizer

T0 = datetime(2026, 11, 18, 0, 0, tzinfo=timezone.utc)


def _cooler(**kw) -> ThresholdConstraints:
    base = dict(
        device_id="aquarium", bottom_threshold=18.0, top_threshold=20.5, unit="°C",
        direction="reduces", rated_power_kw=0.15, active_rate_per_h=0.417,
        drift_rate_per_h=0.185, label="Aquarium",
    )
    return ThresholdConstraints(**{**base, **kw})


def _synthetic(
    true: ThermalModel,
    hours: float = 72,
    modes: tuple[str, ...] = (COOL,),
    ambient: callable | None = None,
    noise: float = 0.0,
    seed: int = 1,
    thresholds: tuple[float, float] = (18.0, 20.5),
) -> tuple[list[tuple[datetime, float]], dict[str, list[tuple[datetime, float]]], list | None]:
    """Minute-by-minute history of a thermostat-controlled device following *true*."""
    rnd = random.Random(seed)
    v = sum(thresholds) / 2
    running = {m: False for m in modes}
    values, switch, amb = [], {m: [] for m in modes}, []
    for k in range(int(hours * 60)):
        t = T0 + timedelta(minutes=k)
        a = ambient(k / 60) if ambient else None
        if COOL in modes:
            if v >= thresholds[1]:
                running[COOL] = True
            elif v <= thresholds[0]:
                running[COOL] = False
        if HEAT in modes:
            heat_on = v <= thresholds[0] + 0.3 and not running.get(COOL)
            running[HEAT] = heat_on
        for m in modes:
            switch[m].append((t, 1.0 if running[m] else 0.0))
        values.append((t, v + rnd.gauss(0, noise)))
        if a is not None:
            amb.append((t, a))
        v += true.rate(v, a, {m: 1.0 if running[m] else 0.0 for m in modes}) / 60
    return values, switch, amb or None


def _windows(true: ThermalModel, **kw) -> list[Window]:
    values, switch, amb = _synthetic(true, **kw)
    end = values[-1][0]
    return build_windows(
        StepSeries(values), {m: StepSeries(s) for m, s in switch.items()}, T0, end,
        StepSeries(amb) if amb else None,
    )


class TestFit:
    def test_constant_rates_are_recovered(self) -> None:
        true = ThermalModel(offset=0.185, gains={COOL: -0.6})
        model, why = fit(_windows(true), [COOL])
        assert why == "ok" and model.form == "constant"
        assert model.offset == pytest.approx(0.185, abs=0.01)
        assert model.running_rate(COOL, 19.0) == pytest.approx(-0.415, abs=0.02)

    def test_ambient_form_is_recovered_when_ambient_varies(self) -> None:
        import math
        true = ThermalModel(offset=0.05, gains={COOL: -0.8}, a=0.08, form="ambient")
        ambient = lambda h: 22.0 + 4.0 * math.sin(h / 24 * 2 * math.pi)  # noqa: E731
        model, why = fit(_windows(true, hours=96, ambient=ambient), [COOL])
        assert why == "ok" and model.form == "ambient"
        assert model.a == pytest.approx(0.08, abs=0.01)
        assert model.gains[COOL] == pytest.approx(-0.8, abs=0.03)

    def test_constant_ambient_falls_back_to_the_constant_form(self) -> None:
        true = ThermalModel(offset=0.185, gains={COOL: -0.6})
        model, _ = fit(_windows(true, ambient=lambda h: 21.0), [COOL])
        assert model.form == "constant"

    def test_two_modes_are_fitted_separately(self) -> None:
        import math
        true = ThermalModel(offset=0.0, gains={COOL: -1.0, HEAT: 0.9}, a=0.1, form="ambient")
        ambient = lambda h: 20.0 + 8.0 * math.sin(h / 24 * 2 * math.pi)  # noqa: E731
        model, why = fit(
            _windows(true, hours=120, modes=(COOL, HEAT), ambient=ambient,
                     thresholds=(19.0, 21.0)),
            [COOL, HEAT],
        )
        assert why == "ok"
        assert model.gains[COOL] == pytest.approx(-1.0, abs=0.05)
        assert model.gains[HEAT] == pytest.approx(0.9, abs=0.05)

    def test_too_little_history_gives_no_model(self) -> None:
        true = ThermalModel(offset=0.185, gains={COOL: -0.6})
        model, why = fit(_windows(true, hours=3), [COOL])
        assert model is None and "windows" in why

    def test_a_cooler_that_does_not_cool_is_rejected(self) -> None:
        # Running windows warm faster than idle ones: wrong wiring / sensor.
        ws = [Window(T0 + timedelta(minutes=15 * i), 0.25, 20.0, 20.0 + (0.1 if i % 3 else 0.2) * 0.25,
                     {COOL: 0.0 if i % 3 else 1.0}) for i in range(80)]
        model, why = fit(ws, [COOL])
        assert model is None and "does not cool" in why

    def test_a_water_change_is_dropped_as_outlier(self) -> None:
        true = ThermalModel(offset=0.185, gains={COOL: -0.6})
        ws = _windows(true)
        i = 40
        ws[i] = Window(ws[i].start, ws[i].hours, ws[i].v0, ws[i].v0 - 2.0, ws[i].on)
        model, _ = fit(ws, [COOL])
        assert model.outliers >= 1
        assert model.offset == pytest.approx(0.185, abs=0.02)


class TestWindowsAndModel:
    def test_gaps_are_left_out(self) -> None:
        value = StepSeries([(T0, 20.0), (T0 + timedelta(minutes=20), None),
                            (T0 + timedelta(minutes=40), 20.1)])
        switch = StepSeries([(T0, 0.0)])
        ws = build_windows(value, {COOL: switch}, T0, T0 + timedelta(hours=1))
        # 00:00–00:15 ok; 00:15–00:30 and 00:30–00:45 touch the gap; 00:45–01:00 ok.
        assert [w.start for w in ws] == [T0, T0 + timedelta(minutes=45)]

    def test_running_fraction(self) -> None:
        value = StepSeries([(T0, 20.0)])
        switch = StepSeries([(T0, 0.0), (T0 + timedelta(minutes=5), 1.0)])
        (w,) = build_windows(value, {COOL: switch}, T0, T0 + timedelta(minutes=15))
        assert w.on[COOL] == pytest.approx(2 / 3)

    def test_configured_rates_round_trip(self) -> None:
        m = ThermalModel.from_constraints(_cooler())
        assert m.drift_rate(19.0) == pytest.approx(0.185)
        assert m.running_rate(COOL, 19.0) == pytest.approx(-0.417)
        heater = ThermalModel.from_constraints(_cooler(direction="increases"))
        assert heater.running_rate(HEAT, 19.0) == pytest.approx(0.417)
        assert ThermalModel.from_dict(json.loads(json.dumps(m.to_dict()))) == m

    def test_simulation_restarts_from_the_measurement(self) -> None:
        m = ThermalModel(offset=1.0, gains={COOL: -2.0})
        ws = [Window(T0 + timedelta(hours=i), 1.0, 20.0 + i, 21.0 + i, {COOL: 0.0}) for i in range(8)]
        sim = simulate(m, ws, reset_every=timedelta(hours=6))
        assert sim[:3] == [21.0, 22.0, 23.0]
        assert sim[6] == pytest.approx(27.0)  # restarted at 26.0 + 1.0


class _FakeHA:
    def __init__(self, history: dict[str, list[tuple[datetime, str]]], ambient: str = "21.0") -> None:
        self._history = history
        self._ambient = ambient
        self.history_calls = 0

    async def get_history(self, entity_ids, start, end=None):
        self.history_calls += 1
        return {e: self._history.get(e, []) for e in entity_ids}

    async def get_entity_state(self, entity_id):
        return self._ambient

    async def get_weather_forecast(self, entity_id, forecast_type="hourly"):
        return [(T0 + timedelta(hours=h), 25.0) for h in range(48)]


def _ha_history(true: ThermalModel, hours: float = 72) -> dict[str, list[tuple[datetime, str]]]:
    values, switch, _ = _synthetic(true, hours=hours)
    return {
        "sensor.temp": [(t, f"{v:.4f}") for t, v in values],
        "switch.cooler": [(t, "on" if s else "off") for t, s in switch[COOL]],
    }


class TestLearner:
    def _learner(self, ha, tmp_path: Path | None = None) -> ThermalLearner:
        return ThermalLearner(
            _cooler(), ha, value_entity="sensor.temp", mode_entities={COOL: "switch.cooler"},
            model_path=(tmp_path / "thermal_aquarium.json") if tmp_path else None,
        )

    async def test_refit_learns_and_persists(self, tmp_path: Path) -> None:
        true = ThermalModel(offset=0.2, gains={COOL: -0.7})
        ha = _FakeHA(_ha_history(true))
        learner = self._learner(ha, tmp_path)
        assert learner.model().source == "configured"
        model = await learner.refit(now=T0 + timedelta(hours=72))
        assert model is not None and learner.model().source == "learned"
        assert learner.model().offset == pytest.approx(0.2, abs=0.02)
        # A restart picks the saved model up again.
        assert self._learner(ha, tmp_path).model() == learner.model()

    async def test_failed_fit_keeps_the_configured_rates(self) -> None:
        ha = _FakeHA({"sensor.temp": [(T0, "20.0")], "switch.cooler": [(T0, "off")]})
        learner = self._learner(ha)
        assert await learner.refit(now=T0 + timedelta(hours=2)) is None
        diag = await learner.diagnostics()
        assert diag["status"] == "fallback" and "windows" in diag["last_error"]
        assert diag["model"]["source"] == "configured"

    async def test_refit_only_when_due(self) -> None:
        true = ThermalModel(offset=0.2, gains={COOL: -0.7})
        ha = _FakeHA(_ha_history(true))
        learner = self._learner(ha)
        now = T0 + timedelta(hours=72)
        await learner.maybe_refit(now)
        await learner.maybe_refit(now + timedelta(hours=1))
        assert ha.history_calls == 1
        await learner.maybe_refit(now + timedelta(hours=7))
        assert ha.history_calls == 2

    async def test_diagnostics_payload(self) -> None:
        true = ThermalModel(offset=0.2, gains={COOL: -0.7})
        learner = self._learner(_FakeHA(_ha_history(true)))
        await learner.refit(now=T0 + timedelta(hours=72))
        diag = await learner.diagnostics()
        assert diag["kind"] == "thermal" and diag["id"] == "thermal:aquarium"
        assert diag["status"] == "learned"
        assert diag["rates"]["drift_per_h"] == pytest.approx(0.2, abs=0.02)
        assert diag["configured"]["running_per_h"][COOL] == pytest.approx(-0.417)
        point = diag["series"][10]
        assert {"t", "value", "model", "on", "ambient"} <= set(point)
        errors = [abs(p["value"] - p["model"]) for p in diag["series"]]
        assert sum(errors) / len(errors) < 0.05

    def test_build_from_config(self) -> None:
        tc = _cooler(device_id="aquarium_cooler")
        assets = {"aquarium": {"type": "threshold", "device": "aquarium_cooler"}}
        devices = parse_devices({"aquarium_cooler": {
            "category": "thermal_load", "type": "threshold_homeassistant",
            "capabilities": {"temperature_c": {"entity": "sensor.t"}, "switch": {"entity": "switch.c"}},
            "params": {"rated_power_w": 150},
        }})
        learners = build_thermal_learners([tc], assets, devices, object(), None)
        assert list(learners) == ["aquarium_cooler"]
        assets["aquarium"]["model"] = "fixed"
        assert build_thermal_learners([tc], assets, devices, object(), None) == {}
        del assets["aquarium"]["model"]
        assert build_thermal_learners([tc], assets, devices, None, None) == {}  # no HA backend


class TestPlanner:
    async def test_planner_steps_the_ambient_model(self) -> None:
        optimizer = MilpHigsOptimizer(step_minutes=60)
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        tc = _cooler(bottom_threshold=24.0, top_threshold=28.0, active_rate_per_h=2.0,
                     drift_rate_per_h=1.0)
        model = ThermalModel(offset=0.0, gains={COOL: -2.0}, a=0.2, form="ambient")
        ctx = OptimizationContext(
            device_states={"aquarium": DeviceState(device_id="aquarium",
                                                    extra={"measured_value": 26.0})},
            threshold_constraints=[tc],
            thermal_models={"aquarium": model},
            ambient_forecasts={"aquarium": [ForecastPoint(timestamp=now, value=30.0)]},
            forecasts={ForecastQuantity.PRICE: [
                ForecastPoint(timestamp=now + timedelta(hours=h), value=0.2 + 0.01 * (h % 5))
                for h in range(24)]},
            horizon=timedelta(hours=12),
        )
        plan = await optimizer.optimize(ctx)
        intents = sorted((i for i in plan.intents if i.device_id == "aquarium"),
                         key=lambda i: i.timestep)
        v, dur = 26.0, [f.hours for f in plan.flows]
        for intent, h in zip(intents, dur):
            run = 1.0 if intent.power_kw > 0 else 0.0
            v = v + h * (0.2 * (30.0 - v) - 2.0 * run)
            assert intent.stored_energy_kwh == pytest.approx(v, abs=1e-3)
            assert 24.0 - 1e-6 <= v <= 28.0 + 1e-6


class TestApi:
    async def test_learned_endpoint_lists_and_refits_thermal_models(self, tmp_path: Path) -> None:
        import httpx

        from energy_assistant.core.config import AppConfig
        from energy_assistant.server import Application

        true = ThermalModel(offset=0.2, gains={COOL: -0.7})
        learner = ThermalLearner(
            _cooler(), _FakeHA(_ha_history(true, hours=72)),
            value_entity="sensor.temp", mode_entities={COOL: "switch.cooler"},
        )
        app = Application.__new__(Application)
        app._cfg = AppConfig()
        app._forecast_providers = []
        app._thermal_learners = {"aquarium": learner}
        app._efficiency_learners = {}
        app._horizon = timedelta(hours=24)

        async def _no_plan() -> None:
            return None
        app._run_plan = _no_plan

        transport = httpx.ASGITransport(app=app._build_api(), client=("127.0.0.1", 1))
        async with httpx.AsyncClient(transport=transport, base_url="http://ea") as c:
            (item,) = (await c.get("/api/learned")).json()
            assert item["kind"] == "thermal" and item["status"] == "fallback"
            r = await c.post("/api/learned/thermal%3Aaquarium/refit")
            assert r.status_code == 200, r.text
            (item,) = (await c.get("/api/learned")).json()
            assert item["status"] == "learned"
            assert (await c.post("/api/learned/thermal%3Anope/refit")).status_code == 404


async def test_ha_client_get_history_parses_minimal_response() -> None:
    import httpx

    from energy_assistant.plugins._homeassistant.client import HAClient

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json=[
            [
                {"entity_id": "sensor.t", "state": "19.875", "last_changed": "2026-11-18T00:00:00+00:00"},
                {"state": "19.9375", "last_changed": "2026-11-18T00:03:10.5+00:00"},
                {"state": "unavailable", "last_changed": "2026-11-18T00:05:00+00:00"},
            ],
            [{"entity_id": "switch.c", "state": "off", "last_changed": "2026-11-18T00:00:00+00:00"}],
        ])

    client = HAClient("http://ha", "token")
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ha")
    hist = await client.get_history(["sensor.t", "switch.c"], start=T0, end=T0 + timedelta(hours=1))
    await client.close()
    assert seen["path"].startswith("/api/history/period/2026-11-18T00:00:00")
    assert seen["params"]["filter_entity_id"] == "sensor.t,switch.c"
    assert "minimal_response" in seen["params"] and "no_attributes" in seen["params"]
    assert [s for _, s in hist["sensor.t"]] == ["19.875", "19.9375", "unavailable"]
    assert hist["sensor.t"][1][0] == datetime(2026, 11, 18, 0, 3, 10, 500000, tzinfo=timezone.utc)
    assert hist["switch.c"] == [(T0, "off")]
