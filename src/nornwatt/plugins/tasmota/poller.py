"""Poll a Tasmota device on the LAN for its sensor values (``Status 10``).

``GET http://<host>/cm?cmnd=Status%2010`` answers with ``{"StatusSNS": {...}}``
in 80-250 ms; values are as fresh as the device's sensor loop (about once a
second for SML meter scripts).  With a web password set on the device the
request carries ``user=…&password=…``; without it Tasmota answers
``{"WARNING": "Need user=<username>&password=<password>"}``.

The poller runs as a background task, started on first use.  Every sample is
stamped with the UTC receive time (Tasmota's ``Time`` is local time without
an offset).  Bad samples — timeouts, HTTP errors, invalid JSON, a missing or
non-numeric power value — are skipped and the last good reading is kept; the
reading becomes unavailable only after ``stale_after_s`` without a valid
sample, so single failures don't make the device flap.  While the device is
unreachable the poll interval backs off exponentially.

Right after a reboot, SML scripts report zeros until the first meter telegram
arrives.  A sample in which a mapped energy counter is exactly 0 after it was
non-zero is therefore skipped as well — unless it stays 0 for half of
``stale_after_s``, which is a real counter reset (``EnergyReset`` on a plug).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from .mapping import (
    FieldMap,
    PayloadError,
    TasmotaReading,
    map_payload,
    suggest_power_paths,
)
from .profile import matching_profiles

_log = logging.getLogger(__name__)
_monotonic = time.monotonic  # module-level so tests can patch the clock

_HTTP_TIMEOUT_S = 5.0
_BACKOFF_MAX_S = 60.0
_INVALID_WARN_STREAK = 10  # warn once after this many unusable samples in a row


class TasmotaError(Exception):
    """The device did not answer with a usable payload (HTTP error, auth, bad JSON)."""


class TasmotaPoller:
    """Background poller for one Tasmota device."""

    def __init__(
        self,
        host: str,
        fields: FieldMap,
        *,
        user: str = "admin",
        password: str | None = None,
        poll_interval_s: float = 2.0,
        stale_after_s: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        base = host if "://" in host else f"http://{host}"
        self._base_url = base.rstrip("/")
        self._fields = fields
        self._params = {"cmnd": "Status 10"}
        if password:
            self._params.update(user=user, password=password)
        self._poll_interval_s = max(0.5, float(poll_interval_s))
        self._stale_after_s = max(self._poll_interval_s * 2, float(stale_after_s))
        self._transport = transport
        self._task: asyncio.Task[None] | None = None

        self._latest: TasmotaReading | None = None
        self._latest_mono = 0.0
        self._was_stale = False
        self._zero_since: float | None = None

        self.invalid_streak = 0
        self.invalid_total = 0
        self.error_streak = 0
        self._suggested = False
        self._sensor_logged: str | None = None

    @property
    def label(self) -> str:
        return self._base_url

    # ------------------------------------------------------------------
    # lifecycle

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"tasmota:{self.label}")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    # ------------------------------------------------------------------
    # readings

    def latest(self) -> TasmotaReading | None:
        """The last valid reading, or None when there is none from the last ``stale_after_s``."""
        if self._latest is None:
            return None
        stale = _monotonic() - self._latest_mono > self._stale_after_s
        if stale and not self._was_stale:
            _log.warning(
                "Tasmota %s: no valid reading for %.0f s — device unavailable",
                self.label, self._stale_after_s,
            )
        self._was_stale = stale
        return None if stale else self._latest

    # ------------------------------------------------------------------
    # polling

    async def _run(self) -> None:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S, transport=self._transport) as client:
            while True:
                delay = await self.poll_once(client)
                await asyncio.sleep(delay)

    async def poll_once(self, client: httpx.AsyncClient) -> float:
        """Fetch and map one sample; return the delay until the next poll."""
        try:
            payload = await self._fetch(client)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — network errors of any kind
            self._record_error(exc)
            return min(self._poll_interval_s * 2 ** self.error_streak, _BACKOFF_MAX_S)

        if self.error_streak:
            _log.info("Tasmota %s reachable again", self.label)
        self.error_streak = 0
        self.handle_payload(payload)
        return self._poll_interval_s

    def handle_payload(self, payload: Any, received_at: datetime | None = None) -> None:
        """Map one decoded payload and keep it when valid."""
        try:
            reading = map_payload(
                payload, self._fields, received_at=received_at or datetime.now(UTC)
            )
            self._check_counters(reading)
        except PayloadError as exc:
            self._record_invalid(exc, payload)
            return
        if reading.sensor != self._fields.sensor and reading.sensor != self._sensor_logged:
            self._sensor_logged = reading.sensor
            _log.info(
                "Tasmota %s: no sensor %r in the payload — reading %r instead",
                self.label, self._fields.sensor, reading.sensor,
            )
        if self.invalid_streak >= _INVALID_WARN_STREAK:
            _log.info("Tasmota %s: valid readings again", self.label)
        self.invalid_streak = 0
        self._latest = reading
        self._latest_mono = _monotonic()

    def _check_counters(self, reading: TasmotaReading) -> None:
        last = self._latest
        dropped = last is not None and any(
            getattr(reading, name) == 0 and getattr(last, name)
            for name in ("energy_in_kwh", "energy_out_kwh")
        )
        if not dropped:
            self._zero_since = None
            return
        now = _monotonic()
        if self._zero_since is None:
            self._zero_since = now
        if now - self._zero_since < self._stale_after_s / 2:
            raise PayloadError("energy counter dropped to 0 (device restarting?)")
        # Still 0 after half the stale timeout: a real counter reset (plug "EnergyReset").
        _log.info("Tasmota %s: energy counter was reset to 0", self.label)
        self._zero_since = None

    async def _fetch(self, client: httpx.AsyncClient) -> Any:
        resp = await client.get(f"{self._base_url}/cm", params=self._params)
        if resp.status_code == 401:
            raise TasmotaError("HTTP 401 — wrong web password")
        resp.raise_for_status()
        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise TasmotaError(f"answer is not JSON: {resp.text[:80]!r}") from exc
        if isinstance(payload, dict) and "WARNING" in payload:
            raise TasmotaError(f"{payload['WARNING']} — set 'password' (the device's web password)")
        return payload

    def _record_error(self, exc: Exception) -> None:
        self.error_streak += 1
        # Log the first failure and then only every 10th, so an outage doesn't flood the log.
        if self.error_streak == 1 or self.error_streak % 10 == 0:
            _log.warning(
                "Tasmota %s: no data (%d in a row): %s",
                self.label, self.error_streak, exc or type(exc).__name__,
            )

    def _record_invalid(self, exc: PayloadError, payload: Any) -> None:
        self.invalid_streak += 1
        self.invalid_total += 1
        if not self._suggested and self._latest is None and self.invalid_streak == 1:
            # Never had a valid reading: most likely a wrong field path.
            self._suggested = True
            hints = []
            if profiles := matching_profiles(payload):
                hints.append(f"matching profiles: {', '.join(profiles)}")
            if paths := suggest_power_paths(payload):
                hints.append(f"power candidates in the payload: {', '.join(paths)}")
            _log.warning(
                "Tasmota %s: %s%s", self.label, exc, f" — {'; '.join(hints)}" if hints else ""
            )
        elif self.invalid_streak == _INVALID_WARN_STREAK:
            _log.warning(
                "Tasmota %s: %d unusable readings in a row (last: %s)",
                self.label, self.invalid_streak, exc,
            )
        else:
            _log.debug("Tasmota %s: skipping reading: %s", self.label, exc)
