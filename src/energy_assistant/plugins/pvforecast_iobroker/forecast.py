"""PvForecastIoBrokerForecast — PV generation forecast from ioBroker pvforecast adapter.

Reads ``pvforecast.0.plants.<plant>.JSONData`` which contains an array of::

    {"t": 1774166400000, "y": 2014}

where ``t`` is a Unix timestamp in **milliseconds** (UTC) and ``y`` is power
in **Watts**.  The provider converts to kW to match the
``ForecastQuantity.PV_GENERATION`` convention used by the optimizer.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from ...core.models import ForecastPoint, ForecastQuantity
from .._iobroker.client import IoBrokerClientProtocol

_log = logging.getLogger(__name__)


class PvForecastIoBrokerForecast:
    """``ForecastProvider`` backed by the ioBroker pvforecast adapter.

    Parameters
    ----------
    forecast_id:
        Stable identifier for this provider (e.g. ``"pv"``).
    client:
        Open ioBroker simple-api client.
    oid:
        Full ioBroker OID for the JSONData value, e.g.
        ``"pvforecast.0.plants.pv.JSONData"``.
    """

    def __init__(
        self,
        forecast_id: str,
        client: IoBrokerClientProtocol,
        oid: str,
    ) -> None:
        self._forecast_id = forecast_id
        self._client = client
        self._oid = oid

    @property
    def quantity(self) -> ForecastQuantity:
        return ForecastQuantity.PV_GENERATION

    async def get_forecast(self, horizon: timedelta) -> list[ForecastPoint]:
        """Fetch the JSONData OID and return forecast points within *horizon*.

        Returns an empty list on any error so the optimizer can fall back
        gracefully to zero PV.
        """
        try:
            raw = await self._client.get_value(self._oid)
        except Exception as exc:
            _log.warning("PvForecastIoBrokerForecast %r: failed to read %r: %s",
                         self._forecast_id, self._oid, exc)
            return []

        if raw is None:
            _log.warning("PvForecastIoBrokerForecast %r: OID %r returned None",
                         self._forecast_id, self._oid)
            return []

        try:
            rows: list[dict] = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError) as exc:
            _log.warning("PvForecastIoBrokerForecast %r: could not parse JSON: %s",
                         self._forecast_id, exc)
            return []

        now = datetime.now(timezone.utc)
        cutoff = now + horizon
        points: list[ForecastPoint] = []

        for row in rows:
            try:
                ts = datetime.fromtimestamp(int(row["t"]) / 1000.0, tz=timezone.utc)
                power_kw = float(row["y"]) / 1000.0
            except (KeyError, ValueError, TypeError):
                continue
            if ts < now or ts > cutoff:
                continue
            points.append(ForecastPoint(timestamp=ts, value=power_kw))

        points.sort(key=lambda p: p.timestamp)
        return points
