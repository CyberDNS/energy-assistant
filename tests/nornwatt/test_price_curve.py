"""Tests for the dashboard price curve (effective price, history, MQTT payloads)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from nornwatt.core.config import MqttConfig
from nornwatt.core.price_curve import (
    PriceCurveHistory,
    PriceSlot,
    build_price_slots,
    current_slot,
    effective_price,
    price_curve_payload,
)
from nornwatt.plugins.mqtt_publisher import (
    _PRICE_ATTR_TOPIC,
    _PRICE_TOPIC,
    EvMqttPublisher,
    _price_discovery_configs,
)

T0 = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def _slot(minutes: int, price: float = 0.4, eff: float | None = 0.3, est: bool = False) -> PriceSlot:
    return PriceSlot(T0 + timedelta(minutes=minutes), price, 0.08, eff, est)


@pytest.mark.parametrize(
    ("pv", "load", "expected"),
    [
        (0.0, 1.0, 0.40),    # no PV → import price
        (0.5, 1.0, 0.24),    # PV covers half → blend
        (3.0, 1.0, 0.08),    # surplus → feed-in price
        (0.0, 0.0, 0.40),    # nothing at all → import price
        (1.0, 0.0, 0.08),    # PV but no load → feed-in price
        (-0.2, 1.0, 0.40),   # negative PV noise is ignored
    ],
)
def test_effective_price(pv: float, load: float, expected: float) -> None:
    assert effective_price(0.40, 0.08, pv, load) == pytest.approx(expected)


def test_build_price_slots_aligns_series() -> None:
    ts = [T0, T0 + timedelta(minutes=15)]
    slots = build_price_slots(ts, [0.4, 0.3], [0.08, 0.08], [0.0, 2.0], [1.0, 1.0], [False, True])
    assert [s.effective_price for s in slots] == pytest.approx([0.4, 0.08])
    assert [s.estimated for s in slots] == [False, True]


def test_history_keeps_passed_slots_and_replaces_future() -> None:
    hist = PriceCurveHistory()
    hist.update([_slot(0), _slot(15), _slot(30)], T0)
    curve = hist.update([_slot(15, eff=0.1), _slot(30, eff=0.1), _slot(45, eff=0.1)], T0 + timedelta(minutes=15))

    assert [s.timestamp for s in curve] == [T0 + timedelta(minutes=m) for m in (0, 15, 30, 45)]
    assert curve[0].effective_price == 0.3          # passed slot kept
    assert all(s.effective_price == 0.1 for s in curve[1:])


def test_history_fills_unknown_past_with_price_only() -> None:
    hist = PriceCurveHistory()
    past = [(T0 - timedelta(minutes=30), 0.5), (T0 - timedelta(minutes=15), 0.45), (T0, 0.99)]
    curve = hist.update([_slot(0)], T0, past)

    assert [s.import_price for s in curve] == [0.5, 0.45, 0.4]   # plan slot wins at T0
    assert curve[0].effective_price is None


def test_history_drops_slots_older_than_keep() -> None:
    hist = PriceCurveHistory(keep=timedelta(hours=1))
    hist.update([_slot(0)], T0)
    curve = hist.update([_slot(120)], T0 + timedelta(hours=2))
    assert [s.timestamp for s in curve] == [T0 + timedelta(minutes=120)]


def test_payload_is_compact_and_apex_ready() -> None:
    slots = [_slot(0), _slot(15, eff=None, est=True)]
    payload = price_curve_payload(slots, 15)

    assert payload["columns"] == ["time", "price", "effective", "feed_in", "estimated"]
    assert payload["data"][0] == [int(T0.timestamp() * 1000), 0.4, 0.3, 0.08, 0]
    assert payload["data"][1][2:] == [None, None, 1]


def test_two_days_fit_recorder_attribute_limit() -> None:
    slots = [_slot(15 * i, price=0.3456, eff=0.2345) for i in range(192)]
    size = len(json.dumps(price_curve_payload(slots, 15), separators=(",", ":")))
    assert size < 16_000


def test_current_slot() -> None:
    slots = [_slot(0), _slot(15), _slot(30)]
    assert current_slot(slots, T0 + timedelta(minutes=20)).timestamp == T0 + timedelta(minutes=15)
    assert current_slot(slots, T0 - timedelta(minutes=1)) is None


def test_price_discovery_configs() -> None:
    configs = dict(_price_discovery_configs())
    price = configs["homeassistant/sensor/nw_electricity_price/config"]
    eff = configs["homeassistant/sensor/nw_effective_electricity_price/config"]

    assert price["json_attributes_topic"] == _PRICE_ATTR_TOPIC
    assert "json_attributes_topic" not in eff
    assert price["state_topic"] == eff["state_topic"] == _PRICE_TOPIC
    assert price["unit_of_measurement"] == "EUR/kWh"


async def test_publisher_queues_curve_then_dedups_state() -> None:
    pub = EvMqttPublisher(
        MqttConfig(host="localhost"), [], _noop_stage, _noop, _noop,
    )
    now = datetime.now(timezone.utc)
    slots = [PriceSlot(now - timedelta(minutes=5), 0.41, 0.08, 0.2, False),
             PriceSlot(now + timedelta(minutes=10), 0.5, 0.08, 0.5, False)]

    await pub.publish_price_curve(slots, 15)
    await pub.publish_current_price()            # unchanged → not queued again

    msgs = []
    while not pub._queue.empty():
        msgs.append(pub._queue.get_nowait())
    assert [m[0] for m in msgs] == [_PRICE_ATTR_TOPIC, _PRICE_TOPIC]
    assert json.loads(msgs[1][1]) == {"price": 0.41, "effective": 0.2}
    assert all(m[2] for m in msgs)               # retained


async def _noop(*_args) -> None:  # noqa: ANN002
    return None


async def _noop_stage(*_args) -> None:  # noqa: ANN002
    return None
