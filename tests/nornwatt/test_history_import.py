"""Past data from Home Assistant (core/history_import): conversion, the checks
before anything is imported, storage, and learning from imported hours."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nornwatt.core.history_import import (
    MIN_OVERLAP_HOURS,
    HistoryImporter,
    HourRow,
    check_against_measured,
    check_battery_physics,
    hour_rows,
)
from nornwatt.core.models import StorageConstraints
from nornwatt.plugins.learned.efficiency import EfficiencyLearner
from nornwatt.storage.sqlite import HistoryRow, SqliteStorageBackend

T0 = datetime(2026, 9, 1, tzinfo=UTC)
CAP = 8.0


def battery_hours(days: int, *, eta: float = 0.9, self_w: float = 30.0, start: datetime = T0,
                  sign: float = 1.0) -> tuple[list[dict], list[dict]]:
    """Hourly statistics (``start``, ``mean``, ``min``, ``max``) of a battery's
    power (W, + charging × *sign*) and SoC (%), as Home Assistant keeps them."""
    power, soc = [], []
    stored, t = CAP * 0.3, start
    for day in range(days):
        plan = [0.0] * 24
        for h in range(9, 9 + 1 + day % 3):     # 2–4 kWh charged around noon
            plan[h] = 2000.0
        for h in range(18, 18 + 1 + day % 2):   # 1–2 kWh discharged in the evening
            plan[h] = -1000.0
        for p in plan:
            before = stored
            stored += (p / 1000 * eta if p > 0 else p / 1000 / eta) - self_w / 1000
            stored = max(0.0, stored)
            lo, hi = sorted((before, stored))
            power.append({"start": t, "mean": sign * p, "min": sign * p, "max": sign * p})
            soc.append({"start": t, "mean": round((before + stored) / 2 / CAP * 100, 1),
                        "min": round(lo / CAP * 100, 1), "max": round(hi / CAP * 100, 1)})
            t += timedelta(hours=1)
    return power, soc


# -- conversion --------------------------------------------------------------------


def test_hours_split_by_direction_and_mixed_hours_count_net():
    t = T0
    rows = hour_rows([
        {"start": t, "mean": 800.0, "min": 0.0, "max": 1200.0},                          # charging
        {"start": t + timedelta(hours=1), "mean": -500.0, "min": -900.0, "max": 0.0},    # discharging
        {"start": t + timedelta(hours=2), "mean": 100.0, "min": -800.0, "max": 1500.0},  # both: net
    ], [
        {"start": t, "mean": 40.0, "min": 35.0, "max": 45.0},   # moving: no reading
        {"start": t + timedelta(hours=1), "mean": 44.5, "min": 44.0, "max": 45.0},  # settled
    ])
    assert [(r.pos_w, r.neg_w) for r in rows] == [(800.0, 0.0), (0.0, 500.0), (100.0, 0.0)]
    assert [r.soc_pct for r in rows] == [None, 44.5, None]


def test_inverting_swaps_the_directions():
    (row,) = hour_rows([{"start": T0, "mean": -700.0, "min": -900.0, "max": 0.0}], None, invert=True)
    assert (row.pos_w, row.neg_w) == (700.0, 0.0)


# -- checks --------------------------------------------------------------------------


def _measured(rows: list[HourRow], scale: float = 1.0) -> list[HistoryRow]:
    return [HistoryRow(r.timestamp, None if r.pos_w is None else r.pos_w * scale,
                       None if r.neg_w is None else r.neg_w * scale, r.soc_pct) for r in rows]


@pytest.mark.parametrize(("sign", "verdict"), [(1.0, "ok"), (-1.0, "inverted")])
def test_matching_entity_is_accepted_and_an_inverted_sign_found(sign, verdict):
    power, soc = battery_hours(5)
    truth = hour_rows(power, soc)
    raw = hour_rows([{**r, "mean": sign * r["mean"], "min": sign * r["max"] if sign < 0 else r["min"],
                      "max": sign * r["min"] if sign < 0 else r["max"]} for r in power], soc)
    check = check_against_measured(raw, _measured(truth), with_soc=True)
    assert check.verdict == verdict
    assert check.overlap_hours >= MIN_OVERLAP_HOURS and abs(check.correlation) > 0.99


def test_a_different_quantity_or_unit_is_refused():
    power, soc = battery_hours(5)
    truth = hour_rows(power, soc)
    check = check_against_measured(truth, _measured(truth, scale=1000.0), with_soc=False)
    assert check.verdict == "mismatch" and "a different quantity or unit" in check.message


def test_an_unrelated_entity_is_refused():
    power, _ = battery_hours(5)
    truth = hour_rows(power, None)
    shuffled = [HourRow(r.timestamp, o.pos_w, o.neg_w, None)
                for r, o in zip(truth, truth[7:] + truth[:7], strict=True)]
    check = check_against_measured(shuffled, _measured(truth), with_soc=False)
    assert check.verdict == "mismatch" and "does not follow" in check.message


def test_a_wrong_state_of_charge_is_refused():
    power, soc = battery_hours(5)
    truth = hour_rows(power, soc)
    off = hour_rows(power, [{**r, "mean": r["mean"] + 20, "min": r["min"] + 20, "max": r["max"] + 20}
                            for r in soc])
    check = check_against_measured(off, _measured(truth), with_soc=True)
    assert check.verdict == "mismatch" and "state of charge" in check.message


def test_without_enough_overlap_it_waits():
    power, soc = battery_hours(5)
    truth = hour_rows(power, soc)
    check = check_against_measured(truth, _measured(truth[:10]), with_soc=True)
    assert check.verdict == "waiting"


@pytest.mark.parametrize(("sign", "verdict"), [(1.0, "ok"), (-1.0, "inverted")])
def test_battery_physics_finds_the_sign_without_overlap(sign, verdict):
    power, soc = battery_hours(10, sign=sign)
    assert check_battery_physics(hour_rows(power, soc)).verdict == verdict


# -- the job, with a real database ------------------------------------------------------


class FakeHA:
    def __init__(self, stats: dict[str, list[dict]]) -> None:
        self.stats = stats

    async def get_statistics(self, ids, start, end=None, period="hour", types=(), units=None):
        return {i: [r for r in self.stats.get(i, []) if start <= r["start"] < end] for i in ids}


@pytest.fixture
async def storage(tmp_path: Path):
    b = SqliteStorageBackend(tmp_path / "h.db")
    await b.start()
    yield b
    await b.stop()


async def _store_measured(storage: SqliteStorageBackend, rows: list[HourRow]) -> None:
    """Hourly rows as if Nornwatt had measured them (no source)."""
    await storage._db.executemany(
        "INSERT INTO history_1h (device_id, timestamp, pos_w, neg_w, soc_pct, samples) "
        "VALUES ('bat', ?, ?, ?, ?, 12)",
        [(r.timestamp.isoformat(), r.pos_w, r.neg_w, r.soc_pct) for r in rows],
    )
    await storage._db.commit()


async def test_import_checks_stores_and_never_replaces_measured_hours(storage, tmp_path):
    power, soc = battery_hours(20, sign=-1.0)           # the entity reports discharging as +
    truth = hour_rows(*battery_hours(20))
    measured = truth[-72:]                              # Nornwatt measured the last 3 days
    await _store_measured(storage, [HourRow(r.timestamp, r.pos_w, r.neg_w, 50.0) if i == 0 else r
                                    for i, r in enumerate(measured)])
    now = truth[-1].timestamp + timedelta(hours=1)
    importer = HistoryImporter("bat", "battery", {"power": "sensor.p", "soc": "sensor.s"},
                               FakeHA({"sensor.p": power, "sensor.s": soc}), storage, tmp_path)
    status = await importer.run(now)
    assert status.state == "imported" and status.inverted
    assert status.hours == len(truth) - len(measured)
    rows = await storage.query_history("bat", T0, now, hourly=True)
    assert len(rows) == len(truth)
    # A measured hour is kept (its marker SoC of 50 % is still there) …
    assert rows[-72].soc_pct == 50.0 and not rows[-72].imported
    # … and the imported ones are corrected for the inverted sign.
    first_charge = next(r for r in rows if r.imported and r.pos_w)
    assert first_charge.pos_w == pytest.approx(2000.0)

    # Removing it sticks: the background run does not import it again.
    assert await importer.remove() == status.hours
    assert importer.done and importer.status.state == "removed"
    again = HistoryImporter("bat", "battery", {"power": "sensor.p", "soc": "sensor.s"},
                            FakeHA({}), storage, tmp_path)
    assert again.done  # status kept across a restart
    # Other entities: a fresh start.
    other = HistoryImporter("bat", "battery", {"power": "sensor.q", "soc": "sensor.s"},
                            FakeHA({}), storage, tmp_path)
    assert not other.done


async def test_a_measured_hour_replaces_an_imported_one(storage):
    t = T0
    await storage.import_history("bat", [HourRow(t, 500.0, 0.0, 40.0)])
    await _store_measured_replace(storage, HourRow(t, 600.0, 0.0, 41.0))
    (row,) = await storage.query_history("bat", t, t, hourly=True)
    assert (row.pos_w, row.imported) == (600.0, False)
    # … and an import never replaces a measured hour.
    await storage.import_history("bat", [HourRow(t, 1.0, 0.0, 1.0)])
    (row,) = await storage.query_history("bat", t, t, hourly=True)
    assert row.pos_w == 600.0


async def _store_measured_replace(storage, r: HourRow) -> None:
    # What maintain_history's roll-up does for a measured hour.
    await storage._db.execute(
        "INSERT OR REPLACE INTO history_1h (device_id, timestamp, pos_w, neg_w, soc_pct, samples) "
        "VALUES ('bat', ?, ?, ?, ?, 12)", (r.timestamp.isoformat(), r.pos_w, r.neg_w, r.soc_pct))
    await storage._db.commit()


async def test_a_mismatching_entity_is_refused_and_nothing_stored(storage, tmp_path):
    power, soc = battery_hours(20)
    truth = hour_rows(power, soc)
    await _store_measured(storage, truth[-72:])
    wrong = [{**r, "mean": r["mean"] / 1000, "min": r["min"] / 1000, "max": r["max"] / 1000}
             for r in power]                            # kW reported as W
    importer = HistoryImporter("bat", "battery", {"power": "sensor.p"},
                               FakeHA({"sensor.p": wrong}), storage, tmp_path)
    status = await importer.run(truth[-1].timestamp + timedelta(hours=1))
    assert status.state == "refused" and "different quantity or unit" in status.message
    assert (await storage.imported_history_bounds("bat"))[2] == 0


async def test_the_battery_learner_learns_from_imported_hours(storage, tmp_path):
    """A fresh install: 30 days of hourly history from Home Assistant, no own
    data yet — the battery's physics check accepts it and the energy balance
    learns from the hourly rows."""
    power, soc = battery_hours(30, eta=0.9, self_w=30.0)
    now = power[-1]["start"] + timedelta(hours=1)
    importer = HistoryImporter("bat", "battery", {"power": "sensor.p", "soc": "sensor.s"},
                               FakeHA({"sensor.p": power, "sensor.s": soc}), storage, tmp_path)
    status = await importer.run(now)
    assert status.state == "imported", status.message
    sc = StorageConstraints(device_id="bat", capacity_kwh=CAP, max_charge_kw=3, max_discharge_kw=3,
                            charge_efficiency=0.95, discharge_efficiency=0.95)
    learner = EfficiencyLearner(sc, storage, mode="learned", history_days=60)
    est = await learner.refit(now)
    assert est is not None and est.confident, est
    assert est.efficiency == pytest.approx(0.9, abs=0.02)
    assert est.self_consumption_w == pytest.approx(30.0, abs=8.0)


def test_status_survives_a_restart(tmp_path):
    importer = HistoryImporter("bat", "battery", {"power": "sensor.p"}, FakeHA({}), None, tmp_path)
    asyncio.run(importer.run(T0))   # no statistics: an error status, saved
    again = HistoryImporter("bat", "battery", {"power": "sensor.p"}, FakeHA({}), None, tmp_path)
    assert again.status is not None and again.status.state == "error"


# -- config ---------------------------------------------------------------------------


def test_history_option_per_category():
    from nornwatt.core.device_model import DeviceConfigError, parse_devices

    bat = parse_devices({"z": {
        "category": "battery", "type": "zendure",
        "params": {"capacity_kwh": 5, "max_charge_kw": 1, "max_discharge_kw": 1},
        "history": {"power": "sensor.zendure_power", "soc": "sensor.zendure_soc"},
    }})["z"]
    assert bat.history == {"power": "sensor.zendure_power", "soc": "sensor.zendure_soc"}
    assert bat.to_dict()["history"] == bat.history
    for bad, msg in (({"soc": "sensor.x"}, "unknown field 'soc'"),
                     ({"power": "not an entity"}, "entity id"),
                     ("sensor.x", "must map")):
        with pytest.raises(DeviceConfigError, match=msg):
            parse_devices({"c": {"category": "consumer", "type": "generic_consumer", "history": bad}})


def test_history_survives_the_config_form():
    from nornwatt.config.store import entry_to_form, form_to_entry

    entry = {"category": "battery", "type": "zendure",
             "history": {"power": "sensor.p", "soc": "sensor.s"}}
    form = entry_to_form("z", entry, {})
    assert form["history"] == entry["history"]
    assert form_to_entry(form)["history"] == entry["history"]
    assert "history" not in form_to_entry({**form, "history": {"power": "", "soc": None}})


# -- one entity per direction (charge / discharge) -----------------------------------------


def test_split_entities_keep_both_directions_of_an_hour():
    t = T0
    rows = hour_rows(None, None, split=(
        [{"start": t, "mean": 600.0}, {"start": t + timedelta(hours=1), "mean": 0.0}],
        [{"start": t, "mean": 200.0}, {"start": t + timedelta(hours=1), "mean": 450.0}],
    ))
    assert [(r.pos_w, r.neg_w) for r in rows] == [(600.0, 200.0), (0.0, 450.0)]
    (swapped, _) = hour_rows(None, None, invert=True, split=(
        [{"start": t, "mean": 600.0}, {"start": t + timedelta(hours=1), "mean": 0.0}],
        [{"start": t, "mean": 200.0}, {"start": t + timedelta(hours=1), "mean": 450.0}],
    ))
    assert (swapped.pos_w, swapped.neg_w) == (200.0, 600.0)


async def test_split_source_is_imported_and_learned_from(storage, tmp_path):
    power, soc = battery_hours(30, eta=0.9, self_w=30.0)
    charge = [{"start": r["start"], "mean": max(r["mean"], 0.0)} for r in power]
    discharge = [{"start": r["start"], "mean": max(-r["mean"], 0.0)} for r in power]
    now = power[-1]["start"] + timedelta(hours=1)
    importer = HistoryImporter(
        "bat", "battery",
        {"power": {"import_entity": "sensor.c", "export_entity": "sensor.d"}, "soc": {"entity": "sensor.s"}},
        FakeHA({"sensor.c": charge, "sensor.d": discharge, "sensor.s": soc}), storage, tmp_path)
    assert importer.entities == ["sensor.c", "sensor.d", "sensor.s"]
    status = await importer.run(now)
    assert status.state == "imported" and not status.inverted, status.message
    sc = StorageConstraints(device_id="bat", capacity_kwh=CAP, max_charge_kw=3, max_discharge_kw=3)
    est = await EfficiencyLearner(sc, storage, mode="learned", history_days=60).refit(now)
    assert est.confident and est.efficiency == pytest.approx(0.9, abs=0.02)


async def test_entities_without_older_history_have_nothing_to_import(storage, tmp_path):
    """The entities Nornwatt publishes itself only have history from when it
    measured the device: measured hours win, nothing is left to import."""
    power, soc = battery_hours(5)
    truth = hour_rows(power, soc)
    await _store_measured(storage, truth)
    importer = HistoryImporter("bat", "battery", {"power": "sensor.p", "soc": "sensor.s"},
                               FakeHA({"sensor.p": power, "sensor.s": soc}), storage, tmp_path)
    status = await importer.run(truth[-1].timestamp + timedelta(hours=1))
    assert status.state == "nothing" and status.hours == 0
    assert "older history" in status.message and importer.done


def test_history_sources_take_the_binding_forms():
    from nornwatt.core.device_model import DeviceConfigError, parse_devices

    base = {"category": "battery", "type": "zendure",
            "params": {"capacity_kwh": 5, "max_charge_kw": 1, "max_discharge_kw": 1}}
    for ok in ({"power": "sensor.a"}, {"power": {"entity": "sensor.a"}, "soc": {"entity": "sensor.s"}},
               {"power": {"import_entity": "sensor.c", "export_entity": "sensor.d"}}):
        assert parse_devices({"z": {**base, "history": ok}})["z"].history == ok
    for bad in ({"power": {"import_entity": "sensor.c"}},
                {"soc": {"import_entity": "sensor.c", "export_entity": "sensor.d"}},
                {"power": {"entity": "sensor.a", "invert_sign": True}}):
        with pytest.raises(DeviceConfigError, match="history"):
            parse_devices({"z": {**base, "history": bad}})
