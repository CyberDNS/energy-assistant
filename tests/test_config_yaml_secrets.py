"""Tests for !secret YAML tag integration in YamlConfigManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from energy_manager.config.yaml import YamlConfigManager
from energy_manager.secrets import SecretNotFoundError, SecretsManager


def _write(path: Path, content: str) -> None:
    path.write_text(content)


# ---------------------------------------------------------------------------
# !secret tag resolves from secrets.yaml
# ---------------------------------------------------------------------------


async def test_secret_tag_resolved_from_file(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yaml",
        """
devices:
  - id: solar_inverter
    plugin: energy_manager.plugins.iobroker
    data:
      api_token: !secret iobroker_api_token
      host: 192.168.1.5
""",
    )
    _write(tmp_path / "secrets.yaml", "iobroker_api_token: supersecret\n")

    manager = YamlConfigManager(tmp_path / "config.yaml")
    entries = await manager.load_entries()

    assert entries[0].data["api_token"] == "supersecret"
    # Non-secret field unaffected.
    assert entries[0].data["host"] == "192.168.1.5"


async def test_secret_tag_resolved_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_TIBBER_TOKEN", "env_token_value")
    _write(
        tmp_path / "config.yaml",
        """
devices:
  - id: tibber
    plugin: energy_manager.plugins.tibber
    data:
      token: !secret tibber_token
""",
    )

    manager = YamlConfigManager(tmp_path / "config.yaml")
    entries = await manager.load_entries()
    assert entries[0].data["token"] == "env_token_value"


async def test_env_overrides_file_for_secret_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_MY_TOKEN", "from_env")
    _write(tmp_path / "config.yaml", "devices:\n  - id: x\n    plugin: p\n    data:\n      tok: !secret my_token\n")
    _write(tmp_path / "secrets.yaml", "my_token: from_file\n")

    manager = YamlConfigManager(tmp_path / "config.yaml")
    entries = await manager.load_entries()
    assert entries[0].data["tok"] == "from_env"


async def test_missing_secret_raises_on_load(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yaml",
        """
devices:
  - id: x
    plugin: p
    data:
      token: !secret nonexistent_secret
""",
    )
    # No secrets.yaml, no env var.
    manager = YamlConfigManager(tmp_path / "config.yaml")
    with pytest.raises(SecretNotFoundError):
        await manager.load_entries()


async def test_multiple_secrets_in_one_config(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yaml",
        """
devices:
  - id: iobroker_device
    plugin: energy_manager.plugins.iobroker
    data:
      api_token: !secret iobroker_token
      username: !secret iobroker_user
""",
    )
    _write(tmp_path / "secrets.yaml", "iobroker_token: tok123\niobroker_user: admin\n")

    manager = YamlConfigManager(tmp_path / "config.yaml")
    entries = await manager.load_entries()
    assert entries[0].data["api_token"] == "tok123"
    assert entries[0].data["username"] == "admin"


async def test_config_without_secrets_works_normally(tmp_path: Path) -> None:
    """Config files without any !secret tags must still load correctly."""
    _write(
        tmp_path / "config.yaml",
        """
devices:
  - id: solar
    plugin: energy_manager.plugins.iobroker
    data:
      host: 192.168.1.5
""",
    )
    manager = YamlConfigManager(tmp_path / "config.yaml")
    entries = await manager.load_entries()
    assert entries[0].data["host"] == "192.168.1.5"


async def test_explicit_secrets_manager_is_used(tmp_path: Path) -> None:
    _write(
        tmp_path / "config.yaml",
        "devices:\n  - id: x\n    plugin: p\n    data:\n      tok: !secret s\n",
    )
    custom_secrets = SecretsManager(tmp_path / "custom_secrets.yaml")
    (tmp_path / "custom_secrets.yaml").write_text("s: custom_value\n")

    manager = YamlConfigManager(tmp_path / "config.yaml", secrets=custom_secrets)
    entries = await manager.load_entries()
    assert entries[0].data["tok"] == "custom_value"
