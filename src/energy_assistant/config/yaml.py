"""YamlConfigLoader — parses the 6-section config YAML into an ``AppConfig``.

Expected top-level sections
----------------------------
``backends``   Connection parameters for ioBroker and Home Assistant.
``tariffs``    Pricing models keyed by name, e.g. ``tibber``, ``export``.
``devices``    Device declarations keyed by device_id.
``topology``   Physical meter wiring as a tree.
``assets``     Managed objects (EVs, heat stores) — future.
``optimizer``  Algorithm, horizon, schedule.

Secrets
-------
Sensitive values (API tokens, passwords) belong in ``secrets.yaml``,
referenced with the ``!secret`` tag::

    backends:
      iobroker:
        api_token: !secret iobroker_api_token
      homeassistant:
        token: !secret ha_token

``secrets.yaml`` lives alongside ``config.yaml`` and must be gitignored.
See ``energy_assistant.secrets.SecretsManager`` for the full resolution rules.

Read-only
---------
Changes to ``config.yaml`` require a restart.  Phase 2 will introduce
hot-reloadable JSON entries without changing the rest of the platform.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..core.config import AppConfig, BackendsConfig, HomeAssistantConfig, IoBrokerConfig
from ..secrets import SecretsManager


def _make_loader(secrets: SecretsManager) -> type[yaml.SafeLoader]:
    """Return a SafeLoader subclass that resolves ``!secret`` tags."""

    class _SecretLoader(yaml.SafeLoader):
        pass

    def _secret_constructor(loader: yaml.SafeLoader, node: yaml.ScalarNode) -> str:
        name = loader.construct_scalar(node)
        return secrets.get(name)

    _SecretLoader.add_constructor("!secret", _secret_constructor)
    return _SecretLoader


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
        self._loader = _make_loader(secrets)

    def load(self) -> AppConfig:
        """Parse the YAML file and return a fully-structured ``AppConfig``.

        Returns an empty ``AppConfig`` when the file does not exist.
        """
        if not self._path.exists():
            return AppConfig()

        with self._path.open() as f:
            raw: dict[str, Any] = yaml.load(f, Loader=self._loader) or {}  # noqa: S506

        backends = _parse_backends(raw.get("backends") or {})
        raw_tariffs: dict[str, Any] = raw.get("tariffs") or {}
        default_tariff_id = _find_default_tariff(raw_tariffs)
        return AppConfig(
            backends=backends,
            tariffs=raw_tariffs,
            devices=_normalize_devices(raw.get("devices") or {}),
            forecasts=_normalize_forecasts(raw.get("forecasts") or {}),
            topology=raw.get("topology") or {},
            assets=raw.get("assets") or {},
            optimizer=raw.get("optimizer") or {},
            default_tariff_id=default_tariff_id,
        )


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
            type: pvforecast_iobroker
            oid: "pvforecast.0.plants.pv.JSONData"

    Dict format::

        forecasts:
          pv:
            type: pvforecast_iobroker
            oid: "pvforecast.0.plants.pv.JSONData"
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
            role: meter
            type: generic_iobroker
            ...

    List format (used in config.yaml)::

        devices:
          - id: my_device
            role: meter
            type: generic_iobroker
            ...

    Nested ``source:`` wrappers are also flattened so every plugin factory
    always receives a single flat config dict::

        # before
        my_device:
          role: meter
          source:
            type: generic_iobroker
            power: "..."

        # after normalisation
        my_device:
          role: meter
          type: generic_iobroker
          power: "..."
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
    iobroker: IoBrokerConfig | None = None
    homeassistant: HomeAssistantConfig | None = None

    if iob_cfg := cfg.get("iobroker"):
        iobroker = IoBrokerConfig(
            host=iob_cfg["host"],
            port=int(iob_cfg.get("port", 8087)),
            api_token=iob_cfg.get("api_token"),
            timeout_s=float(iob_cfg.get("timeout_s", 5.0)),
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

    return BackendsConfig(iobroker=iobroker, homeassistant=homeassistant)
