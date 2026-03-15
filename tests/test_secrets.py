"""Tests for SecretsManager."""

from __future__ import annotations

from pathlib import Path

import pytest

from energy_manager.secrets import SecretNotFoundError, SecretsManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_secrets(path: Path, content: str) -> None:
    path.write_text(content)


# ---------------------------------------------------------------------------
# File-based resolution
# ---------------------------------------------------------------------------


def test_reads_secret_from_file(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "iobroker_api_token: abc123\n")
    manager = SecretsManager(p)
    assert manager.get("iobroker_api_token") == "abc123"


def test_multiple_secrets_in_file(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "token_a: val_a\ntoken_b: val_b\n")
    manager = SecretsManager(p)
    assert manager.get("token_a") == "val_a"
    assert manager.get("token_b") == "val_b"


def test_numeric_value_coerced_to_string(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "port: 8087\n")
    manager = SecretsManager(p)
    assert manager.get("port") == "8087"


def test_missing_file_raises_error(tmp_path: Path) -> None:
    manager = SecretsManager(tmp_path / "no_secrets.yaml")
    with pytest.raises(SecretNotFoundError) as exc_info:
        manager.get("some_secret")
    assert "some_secret" in str(exc_info.value)


def test_missing_key_raises_error(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "other_key: value\n")
    manager = SecretsManager(p)
    with pytest.raises(SecretNotFoundError) as exc_info:
        manager.get("missing_key")
    assert "missing_key" in str(exc_info.value)


def test_error_message_contains_env_var_hint(tmp_path: Path) -> None:
    manager = SecretsManager(tmp_path / "secrets.yaml")
    with pytest.raises(SecretNotFoundError) as exc_info:
        manager.get("my_token")
    assert "ENERGY_ASSISTANT_SECRET_MY_TOKEN" in str(exc_info.value)


def test_empty_secrets_file_raises_on_any_get(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "")
    manager = SecretsManager(p)
    with pytest.raises(SecretNotFoundError):
        manager.get("anything")


def test_file_cached_after_first_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "token: original\n")
    manager = SecretsManager(p)
    assert manager.get("token") == "original"

    # Overwrite the file — should still return cached value without reload().
    _write_secrets(p, "token: changed\n")
    assert manager.get("token") == "original"


def test_reload_invalidates_cache(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "token: original\n")
    manager = SecretsManager(p)
    assert manager.get("token") == "original"

    _write_secrets(p, "token: changed\n")
    manager.reload()
    assert manager.get("token") == "changed"


# ---------------------------------------------------------------------------
# Environment variable resolution
# ---------------------------------------------------------------------------


def test_env_var_overrides_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "my_token: from_file\n")
    monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_MY_TOKEN", "from_env")
    manager = SecretsManager(p)
    assert manager.get("my_token") == "from_env"


def test_env_var_works_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_STANDALONE", "env_value")
    manager = SecretsManager(tmp_path / "secrets.yaml")
    assert manager.get("STANDALONE") == "env_value"


def test_env_var_name_case_insensitive_lookup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_MY_TOKEN", "hello")
    manager = SecretsManager(tmp_path / "secrets.yaml")
    # The get() key is lower-case; env var lookup must uppercase it.
    assert manager.get("my_token") == "hello"


def test_env_var_hyphen_becomes_underscore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENERGY_ASSISTANT_SECRET_TIBBER_API_TOKEN", "xyz")
    manager = SecretsManager(tmp_path / "secrets.yaml")
    assert manager.get("tibber-api-token") == "xyz"


def test_env_var_not_set_and_no_file_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ENERGY_ASSISTANT_SECRET_ABSENT", raising=False)
    manager = SecretsManager(tmp_path / "secrets.yaml")
    with pytest.raises(SecretNotFoundError):
        manager.get("absent")


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_invalid_yaml_structure_raises(tmp_path: Path) -> None:
    p = tmp_path / "secrets.yaml"
    _write_secrets(p, "- item1\n- item2\n")  # a list, not a mapping
    manager = SecretsManager(p)
    with pytest.raises(ValueError, match="mapping"):
        manager.get("anything")
