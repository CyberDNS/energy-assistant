"""
YAML-based ConfigManager implementation (Phase 1).

Reads device/integration declarations from a YAML file at startup.
The config is read-only at runtime; changes require a restart.

Secrets
-------
Credentials and other sensitive values should never be stored directly in
``config.yaml``.  Use the ``!secret`` tag to reference a name defined in
``secrets.yaml`` (or an environment variable):

    devices:
      - id: solar_inverter
        plugin: energy_manager.plugins.iobroker
        data:
          host: 192.168.1.5
          port: 8087
          api_token: !secret iobroker_api_token

``secrets.yaml`` lives alongside ``config.yaml`` and must be gitignored.
See ``energy_manager.secrets.SecretsManager`` for the full resolution rules.

Expected YAML structure
-----------------------
    devices:
      - id: solar_inverter
        plugin: energy_manager.plugins.iobroker
        data:
          host: 192.168.1.5
          port: 8087
          state_map:
            power_w: "fronius.0.inverters.0.Power"
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ..core.models import ConfigEntry
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


class YamlConfigManager:
    """
    Read-only config manager backed by a YAML file.

    Parameters
    ----------
    path:
        Path to ``config.yaml``.
    secrets:
        Optional ``SecretsManager`` used to resolve ``!secret`` tags.
        When omitted a ``SecretsManager`` is created automatically, looking for
        ``secrets.yaml`` in the same directory as ``config.yaml``.
    """

    def __init__(self, path: Path | str, secrets: SecretsManager | None = None) -> None:
        self._path = Path(path)
        if secrets is None:
            secrets = SecretsManager(self._path.parent / "secrets.yaml")
        self._secrets = secrets
        self._loader = _make_loader(self._secrets)

    async def load_entries(self) -> list[ConfigEntry]:
        """Parse the YAML file and return all declared device entries."""
        if not self._path.exists():
            return []
        with self._path.open() as f:
            data = yaml.load(f, Loader=self._loader) or {}  # noqa: S506 — loader is our safe subclass
        return [ConfigEntry(**entry) for entry in data.get("devices", [])]

    async def save_entry(self, entry: ConfigEntry) -> None:
        raise NotImplementedError(
            "YamlConfigManager is read-only at runtime. "
            "Edit the YAML file directly and restart."
        )

    async def delete_entry(self, entry_id: str) -> None:
        raise NotImplementedError(
            "YamlConfigManager is read-only at runtime. "
            "Edit the YAML file directly and restart."
        )
