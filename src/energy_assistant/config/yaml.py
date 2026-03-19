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
        return AppConfig(
            backends=backends,
            tariffs=raw.get("tariffs") or {},
            devices=raw.get("devices") or {},
            topology=raw.get("topology") or {},
            assets=raw.get("assets") or {},
            optimizer=raw.get("optimizer") or {},
        )


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
        homeassistant = HomeAssistantConfig(
            url=ha_cfg["url"],
            token=ha_cfg["token"],
            timeout_s=float(ha_cfg.get("timeout_s", 10.0)),
        )

    return BackendsConfig(iobroker=iobroker, homeassistant=homeassistant)
