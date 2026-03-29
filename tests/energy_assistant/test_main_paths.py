"""Tests for CLI path resolution in ``energy_assistant.__main__``."""

from __future__ import annotations

from pathlib import Path

from energy_assistant import __main__ as app_main


class TestParseArgs:
    def test_explicit_positional_config_and_db_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(
            app_main.sys,
            "argv",
            ["energy-assistant", "/tmp/custom.yaml", "--db", "/tmp/custom.db"],
        )

        config_path, db_path = app_main._parse_args()

        assert config_path == Path("/tmp/custom.yaml")
        assert db_path == Path("/tmp/custom.db")

    def test_long_config_flag_and_db_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(
            app_main.sys,
            "argv",
            [
                "energy-assistant",
                "--config",
                "/tmp/flagged.yaml",
                "--db",
                "/tmp/flagged.db",
            ],
        )

        config_path, db_path = app_main._parse_args()

        assert config_path == Path("/tmp/flagged.yaml")
        assert db_path == Path("/tmp/flagged.db")

    def test_environment_defaults_when_no_args(self, monkeypatch) -> None:
        monkeypatch.setattr(app_main.sys, "argv", ["energy-assistant"])
        monkeypatch.setenv("ENERGY_ASSISTANT_CONFIG", "/tmp/from-env.yaml")
        monkeypatch.setenv("ENERGY_ASSISTANT_DB", "/tmp/from-env.db")

        config_path, db_path = app_main._parse_args()

        assert config_path == Path("/tmp/from-env.yaml")
        assert db_path == Path("/tmp/from-env.db")
