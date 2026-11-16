"""Zendure cloud login — trade the app's "HA key" for MQTT broker credentials.

The Zendure app (Profile → Developer / Home Assistant) shows an
*authorization cloud key*: base64 of ``"<api_url>.<app_key>"``, e.g.
``https://app.zendure.tech/eu.<key>``.  POSTing the app key, signed with the
public ``zenHa`` client secret, to ``<api_url>/api/ha/deviceList`` returns the
account's device list plus the credentials for the Zendure cloud MQTT broker.

This is the same handshake the official Zendure Home Assistant integration and
the ioBroker ``zendure-solarflow`` adapter (``connectionMode: authKey``) use.
"""

from __future__ import annotations

import base64
import hashlib
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

# Public client signing secret of the Zendure HA API (clientid "zenHa"), as
# shipped in the open-source Zendure HA integration / ioBroker adapter.  Not a
# per-user secret — the user secret is the app key.
_SIGN_KEY = "C*dafwArEOXK"
_CLIENT_ID = "zenHa"
_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class ZendureMqttCredentials:
    """Cloud MQTT broker connection parameters returned by the login."""

    host: str
    port: int
    client_id: str
    username: str
    password: str


@dataclass(frozen=True)
class ZendureCloudDevice:
    """One entry of the account's device list."""

    product_key: str
    device_key: str
    name: str = ""
    model: str = ""
    serial: str = ""
    ip: str = ""
    online: bool = False


@dataclass(frozen=True)
class ZendureCloudLogin:
    mqtt: ZendureMqttCredentials
    devices: list[ZendureCloudDevice] = field(default_factory=list)


def decode_cloud_key(cloud_key: str) -> tuple[str, str]:
    """Split the base64 authorization cloud key into ``(api_url, app_key)``."""
    try:
        decoded = base64.b64decode(cloud_key.strip(), validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Zendure cloud key is not valid base64") from exc
    api_url, sep, app_key = decoded.rpartition(".")
    if not sep or not api_url.startswith("http") or not app_key:
        raise ValueError("Zendure cloud key does not decode to '<api_url>.<app_key>'")
    return api_url.rstrip("/"), app_key


def sign_headers(body: dict[str, Any], *, timestamp: int, nonce: str) -> dict[str, str]:
    """Build the signed request headers for the Zendure HA API."""
    params: dict[str, Any] = {**body, "timestamp": timestamp, "nonce": nonce}
    body_str = "".join(f"{k}{params[k]}" for k in sorted(params))
    sign = hashlib.sha1(f"{_SIGN_KEY}{body_str}{_SIGN_KEY}".encode()).hexdigest().upper()
    return {
        "Content-Type": "application/json",
        "timestamp": str(timestamp),
        "nonce": nonce,
        "clientid": _CLIENT_ID,
        "sign": sign,
    }


def parse_login_response(payload: dict[str, Any]) -> ZendureCloudLogin:
    """Parse the ``/api/ha/deviceList`` JSON response."""
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("mqtt"), dict):
        msg = payload.get("msg") or payload.get("message") or "no data in response"
        raise RuntimeError(f"Zendure cloud login failed: {msg}")

    m = data["mqtt"]
    url = str(m.get("url", ""))
    host, sep, port = url.rpartition(":")
    if not sep:
        host, port = url, "1883"
    mqtt = ZendureMqttCredentials(
        host=host,
        port=int(port),
        client_id=str(m.get("clientId", "")),
        username=str(m.get("username", "")),
        password=str(m.get("password", "")),
    )
    devices = [
        ZendureCloudDevice(
            product_key=str(d.get("productKey", "")),
            device_key=str(d.get("deviceKey", "")),
            name=str(d.get("deviceName", "")),
            model=str(d.get("productModel", "")),
            serial=str(d.get("snNumber", "")),
            ip=str(d.get("ip", "")),
            online=bool(d.get("online", False)),
        )
        for d in data.get("deviceList") or []
        if isinstance(d, dict)
    ]
    return ZendureCloudLogin(mqtt=mqtt, devices=devices)


async def cloud_login(
    cloud_key: str, *, http: httpx.AsyncClient | None = None
) -> ZendureCloudLogin:
    """Log in with the authorization cloud key and return MQTT credentials."""
    api_url, app_key = decode_cloud_key(cloud_key)
    body = {"appKey": app_key}
    headers = sign_headers(
        body, timestamp=int(time.time()), nonce=str(random.randint(10000, 99999))
    )
    url = f"{api_url}/api/ha/deviceList"
    if http is None:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            resp = await client.post(url, json=body, headers=headers)
    else:
        resp = await http.post(url, json=body, headers=headers)
    resp.raise_for_status()
    return parse_login_response(resp.json())
