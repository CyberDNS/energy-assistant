"""YamlConfigLoader — parses the 6-section config YAML into an ``AppConfig``.

Expected top-level sections
----------------------------
``backends``   Connection parameters for Home Assistant and the MQTT broker.
``tariffs``    Pricing models keyed by name, e.g. ``tibber``, ``export``.
``devices``    Device declarations (category, capabilities, params,
               connections), see ``core/device_model``.
``optimizer``  Algorithm, horizon, schedule.
``energy_dashboard``  Optional import of the HA Energy dashboard consumers.

Secrets
-------
Sensitive values (API tokens, passwords) belong in ``secrets.yaml``,
referenced with the ``!secret`` tag::

    backends:
      homeassistant:
        token: !secret ha_token
      mqtt:
        password: !secret mqtt_password

``secrets.yaml`` lives alongside ``config.yaml`` and must be gitignored.
See ``energy_assistant.secrets.SecretsManager`` for the full resolution rules.

Changes at runtime
------------------
The loader only reads.  Device entries are edited by ``config/store``
(configuration UI); ``server.Application.reload_config`` applies a changed
file without a restart.
"""

from __future__ import annotations

import os
import re
import secrets as secrets_module
from pathlib import Path
from typing import Any

import yaml

from ..core.config import (
    AppConfig,
    BackendsConfig,
    HomeAssistantConfig,
    MqttConfig,
)
from ..core.device_model import DeviceConfigError, parse_devices
from ..secrets import SecretNotFoundError, SecretsManager

#: ``17:00`` and the like.  YAML 1.1 (PyYAML) reads them as base-60 numbers
#: (17:00 → 1020); YAML 1.2 — and ruamel, which writes the file from the
#: config UI without quotes — as strings.  The config means clock times.
_CLOCK_TIME = re.compile(r"^[0-9]+(?::[0-5]?[0-9])+$")


def _make_loader(secrets: SecretsManager | None, missing: list[str]) -> type[yaml.SafeLoader]:
    """Return a SafeLoader subclass that resolves ``!secret`` tags and reads
    ``HH:MM`` as a string, not as a base-60 number.

    A secret that is not defined resolves to ``""`` and its name is added to
    *missing*, so every missing secret is reported, not only the first.
    Without *secrets* every ``!secret`` resolves to ``""`` (``fallback_config``).
    """

    class _SecretLoader(yaml.SafeLoader):
        pass

    _SecretLoader.yaml_implicit_resolvers = {
        first: list(resolvers)
        for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
    }
    for digit in "0123456789":
        _SecretLoader.yaml_implicit_resolvers.setdefault(digit, []).insert(
            0, ("tag:yaml.org,2002:str", _CLOCK_TIME)
        )

    def _secret_constructor(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> str:
        name = str(loader.construct_scalar(node))
        if secrets is None:
            return ""
        try:
            return secrets.get(name)
        except SecretNotFoundError:
            if name not in missing:
                missing.append(name)
            return ""

    _SecretLoader.add_constructor("!secret", _secret_constructor)
    return _SecretLoader


def yaml_error_message(exc: yaml.YAMLError) -> str:
    """A YAML syntax error as one line, with the line and column it is at."""
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    problem = getattr(exc, "problem", None) or getattr(exc, "context", None) or str(exc)
    if mark is None:
        return f"config.yaml is not valid YAML: {problem}"
    return f"config.yaml is not valid YAML: line {mark.line + 1}, column {mark.column + 1}: {problem}"


class YamlConfigLoader:
    """Loads and parses the 6-section YAML config file into an ``AppConfig``.

    Parameters
    ----------
    path:
        Path to ``config.yaml``.
    secrets:
        Optional ``SecretsManager``.  When omitted a manager is created
        automatically, looking for ``secrets.yaml`` next to ``config.yaml``.
    """

    def __init__(self, path: Path | str, secrets: SecretsManager | None = None) -> None:
        self._path = Path(path)
        if secrets is None:
            secrets = SecretsManager(self._path.parent / "secrets.yaml")
        self._missing: list[str] = []
        self._loader = _make_loader(secrets, self._missing)

    def load(self) -> AppConfig:
        """Parse the YAML file and return a fully-structured ``AppConfig``.

        Returns an empty ``AppConfig`` when the file does not exist.
        Raises ``DeviceConfigError`` when the ``devices:`` section is invalid.
        """
        if not self._path.exists():
            return AppConfig()
        return self.load_text(self._path.read_text())

    def load_text(self, text: str) -> AppConfig:
        """Parse *text* as the content of the config file (``!secret`` tags are
        resolved as for the file itself).  Used to check an edited config
        before it is written."""
        self._missing.clear()
        try:
            raw: dict[str, Any] = yaml.load(text, Loader=self._loader) or {}
        except yaml.YAMLError as exc:
            raise DeviceConfigError([yaml_error_message(exc)]) from exc
        if self._missing:
            raise DeviceConfigError([
                f"secret {name!r} is not defined in secrets.yaml" for name in self._missing
            ])

        if "topology" in raw:
            raise DeviceConfigError([(
                "the 'topology:' section was removed — set 'metered_by: <meter id>' on each "
                "device instead, and move max_import_kw / curtail_order / "
                "limit_hysteresis_kw to the 'params:' of the grid connection meter"
            )])
        if raw.get("assets"):
            raise DeviceConfigError([_assets_hint(raw["assets"])])
        old_thermal = [
            device_id for device_id, d in _normalize_devices(raw.get("devices") or {}).items()
            if d.get("type") == "threshold_homeassistant"
        ]
        if old_thermal:
            raise DeviceConfigError([(
                f"devices: {', '.join(old_thermal)}: type 'threshold_homeassistant' was replaced — "
                "a 'category: switch' device (type homeassistant_switch, capabilities.switch, "
                "params.rated_power_w) and the thermal_load with type homeassistant_thermal, "
                "'controlled_by: <switch>' and the thresholds / rates under 'params:'"
            )])
        removed = _removed_types(raw)
        if removed:
            raise DeviceConfigError(removed)
        # Check everything and report all problems together.
        from ..loader.config_validation import validate_sections

        if not isinstance(raw, dict):
            raise DeviceConfigError(["the config file must be a mapping of sections"])
        errors = validate_sections(raw)
        devices = {}
        try:
            devices = parse_devices(_normalize_devices(raw.get("devices") or {}))
        except DeviceConfigError as exc:
            errors.extend(exc.errors)
        if errors:
            raise DeviceConfigError(errors)
        backends = with_supervisor_default(_parse_backends(raw.get("backends") or {}))
        raw_tariffs: dict[str, Any] = raw.get("tariffs") or {}
        default_tariff_id = _find_default_tariff(raw_tariffs)
        return AppConfig(
            backends=backends,
            tariffs=raw_tariffs,
            devices=devices,
            forecasts=_normalize_forecasts(raw.get("forecasts") or {}),
            optimizer=raw.get("optimizer") or {},
            controller=raw.get("controller") or {},
            server=raw.get("server") or {},
            logging=raw.get("logging") or {},
            energy_dashboard=raw.get("energy_dashboard") or {},
            default_tariff_id=default_tariff_id,
        )


#: The Home Assistant Core API as the Supervisor proxies it for an add-on
#: (``homeassistant_api: true`` in the add-on's config.yaml).
SUPERVISOR_CORE_URL = "http://supervisor/core"
SUPERVISOR_CORE_WS_URL = "ws://supervisor/core/websocket"


def supervisor_homeassistant() -> HomeAssistantConfig | None:
    """The Home Assistant this add-on runs in, when running as an add-on
    (the Supervisor sets ``SUPERVISOR_TOKEN``); else ``None``."""
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if not token:
        return None
    return HomeAssistantConfig(
        url=SUPERVISOR_CORE_URL, token=token, ws_url=SUPERVISOR_CORE_WS_URL, supervisor=True,
    )


def with_supervisor_default(backends: BackendsConfig) -> BackendsConfig:
    """*backends* with the add-on's own Home Assistant when none is configured.
    An explicit ``backends.homeassistant`` (e.g. a remote HA) always wins."""
    if backends.homeassistant is None:
        backends.homeassistant = supervisor_homeassistant()
    return backends


def fallback_config(text: str) -> AppConfig:
    """What runs while the config file has problems: no devices, tariffs or
    forecasts — only what keeps the UI usable and safe.

    * ``server`` (port, API token, trusted hosts) and ``logging`` are taken
      over when they can be read.  When the file mentions ``api_token`` but
      its ``server:`` section cannot be read, the API is locked down to the
      trusted hosts (HA ingress, localhost) rather than left open.
    * ``backends.homeassistant`` when it can be read (else the add-on's own
      Home Assistant), so the entity pickers keep working.  MQTT is left
      out: the publisher would remove the entities of the devices that are
      missing from this empty configuration.
    """
    try:
        raw = yaml.load(text, Loader=_make_loader(None, [])) or {}
    except yaml.YAMLError:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}

    def section(name: str) -> dict[str, Any] | None:
        value = raw.get(name)
        if value is None:
            return {}
        return dict(value) if isinstance(value, dict) else None

    server = section("server")
    if server is None or (not server.get("api_token") and "api_token" in text):
        # An unguessable token: only the trusted hosts get in.
        server = {"api_token": secrets_module.token_urlsafe(32)}
    homeassistant = None
    backends = section("backends") or {}
    ha = backends.get("homeassistant")
    if isinstance(ha, dict) and isinstance(ha.get("url"), str) and ha.get("token"):
        try:
            homeassistant = HomeAssistantConfig(
                url=ha["url"], token=str(ha["token"]),
                timeout_s=float(ha.get("timeout_s", 10.0)),
            )
        except (TypeError, ValueError):
            homeassistant = None
    return AppConfig(
        backends=with_supervisor_default(BackendsConfig(homeassistant=homeassistant)),
        server=server,
        logging=section("logging") or {},
    )


#: Device plugin types that were renamed: old → new.
_RENAMED_DEVICE_TYPES = {"tibber": "tibber_cloud"}

#: Plugin types that were removed, with what to use instead.
_REMOVED_TYPES = {
    "generic_iobroker": "homeassistant_sensor (capabilities.power_w), tasmota or tibber_local",
    "sma_modbus_iobroker": "modbus (profile: sma_sunny_boy_storage)",
    "zendure_iobroker": "zendure",
    "tibber_iobroker": "tibber (tariff) or tibber_cloud (meter)",
    "pvforecast_iobroker": "forecast_solar",
}


def _removed_types(raw: dict[str, Any]) -> list[str]:
    """Errors for devices, tariffs and forecasts that use a removed plugin type."""
    sections = {
        "devices": _normalize_devices(raw.get("devices") or {}),
        "tariffs": raw.get("tariffs") or {},
        "forecasts": _normalize_forecasts(raw.get("forecasts") or {}),
    }
    errors = []
    for section, entries in sections.items():
        for entry_id, cfg in entries.items():
            type_name = cfg.get("type") if isinstance(cfg, dict) else None
            if section == "devices" and type_name in _RENAMED_DEVICE_TYPES:
                errors.append(f"devices: {entry_id}: type {type_name!r} was renamed to "
                              f"{_RENAMED_DEVICE_TYPES[type_name]!r}")
            if type_name in _REMOVED_TYPES:
                errors.append(f"{section}: {entry_id}: type {type_name!r} was removed with the "
                              f"ioBroker support — use {_REMOVED_TYPES[type_name]}")
    return errors


def _assets_hint(assets: Any) -> str:
    """Migration hint for a leftover ``assets:`` section."""
    kinds = {
        str(cfg.get("type")) if isinstance(cfg, dict) and cfg.get("type") == "threshold" else "ev"
        for cfg in (assets.values() if isinstance(assets, dict) else [])
    }
    parts = []
    if "ev" in kinds:
        parts.append("EVs are 'category: vehicle' devices (capacity, charge curve, SoC entity) with "
                     "'params: {default_vehicle: <car>, min_power_kw, max_power_kw}' on the charger")
    if "threshold" in kinds:
        parts.append("threshold loads are 'category: thermal_load' devices (thresholds, rates "
                     "and run times under 'params:', 'model: fixed' → 'learning: false') "
                     "'controlled_by' a 'category: switch' device with params.rated_power_w; "
                     "keep the device id so stored models and savings stay")
    return "the 'assets:' section was removed — " + "; ".join(parts)


def _find_default_tariff(raw_tariffs: dict[str, Any]) -> str | None:
    """Return the tariff_id marked with ``default: true``, or ``None``."""
    for tariff_id, cfg in raw_tariffs.items():
        if isinstance(cfg, dict) and cfg.get("default"):
            return tariff_id
    return None


def _normalize_forecasts(raw: Any) -> dict[str, dict[str, Any]]:
    """Accept both dict-format and list-format forecast declarations.

    List format::

        forecasts:
          - id: pv
            type: forecast_solar
            kwp: 7.4

    Dict format::

        forecasts:
          pv:
            type: forecast_solar
            kwp: 7.4
    """
    if isinstance(raw, list):
        result: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            forecast_id = entry.get("id")
            if not forecast_id:
                continue
            result[forecast_id] = {k: v for k, v in entry.items() if k != "id"}
        return result
    if isinstance(raw, dict):
        return dict(raw)
    return {}


def _normalize_devices(raw: Any) -> dict[str, dict[str, Any]]:
    """Accept both dict-format and list-format device declarations.

    Dict format (used in tests)::

        devices:
          my_device:
            category: meter
            type: tasmota
            ...

    List format (used in config.yaml)::

        devices:
          - id: my_device
            category: meter
            type: tasmota
            ...

    Nested ``source:`` wrappers are also flattened so every plugin factory
    always receives a single flat config dict::

        # before
        my_device:
          category: meter
          source:
            type: tasmota
            host: "..."

        # after normalisation
        my_device:
          category: meter
          type: tasmota
          host: "..."
    """
    if isinstance(raw, list):
        result: dict[str, dict[str, Any]] = {}
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            device_id = entry.get("id")
            if not device_id:
                continue
            result[device_id] = _flatten_device_cfg({k: v for k, v in entry.items() if k != "id"})
        return result
    if isinstance(raw, dict):
        return {k: _flatten_device_cfg(v) for k, v in raw.items()}
    return {}


def _flatten_device_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    """Merge a ``source:`` sub-dict into the top-level device config dict.

    Keys from ``source`` overwrite same-named top-level keys so that
    ``source.type`` always wins over a stray top-level ``type``.
    """
    if "source" not in cfg:
        return cfg
    source = cfg.get("source") or {}
    result = {k: v for k, v in cfg.items() if k != "source"}
    result.update(source)
    return result


def _parse_backends(cfg: dict[str, Any]) -> BackendsConfig:
    homeassistant: HomeAssistantConfig | None = None

    if "iobroker" in cfg:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            "backends.iobroker is no longer used (the ioBroker plugins were removed) — "
            "remove it from config.yaml"
        )

    if ha_cfg := cfg.get("homeassistant"):
        token = ha_cfg.get("token", "")
        if not token:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "homeassistant backend configured without a token — HA client will not be built"
            )
        homeassistant = HomeAssistantConfig(
            url=ha_cfg["url"],
            token=token,
            timeout_s=float(ha_cfg.get("timeout_s", 10.0)),
        )

    mqtt: MqttConfig | None = None
    if mqtt_cfg := cfg.get("mqtt"):
        mqtt = MqttConfig(
            host=mqtt_cfg["host"],
            port=int(mqtt_cfg.get("port", 1883)),
            username=mqtt_cfg.get("username"),
            password=mqtt_cfg.get("password"),
        )

    return BackendsConfig(homeassistant=homeassistant, mqtt=mqtt)
