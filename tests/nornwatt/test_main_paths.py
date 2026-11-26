"""Tests for CLI path resolution in ``nornwatt.__main__``."""

from __future__ import annotations

from pathlib import Path

from nornwatt import __main__ as app_main


class TestParseArgs:
    def test_explicit_positional_config_and_db_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(
            app_main.sys,
            "argv",
            ["nornwatt", "/tmp/custom.yaml", "--db", "/tmp/custom.db"],
        )

        config_path, db_path = app_main._parse_args()

        assert config_path == Path("/tmp/custom.yaml")
        assert db_path == Path("/tmp/custom.db")

    def test_long_config_flag_and_db_flag(self, monkeypatch) -> None:
        monkeypatch.setattr(
            app_main.sys,
            "argv",
            [
                "nornwatt",
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
        monkeypatch.setattr(app_main.sys, "argv", ["nornwatt"])
        monkeypatch.setenv("NORNWATT_CONFIG", "/tmp/from-env.yaml")
        monkeypatch.setenv("NORNWATT_DB", "/tmp/from-env.db")

        config_path, db_path = app_main._parse_args()

        assert config_path == Path("/tmp/from-env.yaml")
        assert db_path == Path("/tmp/from-env.db")

    def test_ha_mode_uses_ha_paths(self, monkeypatch, tmp_path: Path) -> None:
        # /config/config.yaml — no subdir, the host folder is already namespaced
        # by {REPO}_{slug} and mounted at /config inside the container.
        monkeypatch.setattr(app_main.sys, "argv", ["nornwatt"])
        monkeypatch.delenv("NORNWATT_CONFIG", raising=False)
        monkeypatch.delenv("NORNWATT_DB", raising=False)
        monkeypatch.setenv("NORNWATT_MODE", "ha")
        ha_cfg = tmp_path / "config" / "config.yaml"
        ha_db = tmp_path / "data" / "nornwatt.db"
        monkeypatch.setattr(app_main, "_HA_CONFIG", ha_cfg)
        monkeypatch.setattr(app_main, "_HA_DB", ha_db)

        config_path, db_path = app_main._parse_args()

        assert config_path == ha_cfg
        assert db_path == ha_db

    def test_local_mode_uses_repo_defaults(self, monkeypatch) -> None:
        monkeypatch.setattr(app_main.sys, "argv", ["nornwatt"])
        monkeypatch.delenv("NORNWATT_CONFIG", raising=False)
        monkeypatch.delenv("NORNWATT_DB", raising=False)
        monkeypatch.setenv("NORNWATT_MODE", "local")

        config_path, db_path = app_main._parse_args()

        assert config_path == Path(app_main._DEFAULT_CONFIG)
        assert db_path == Path(app_main._DEFAULT_DB)


class TestLegacyDb:
    def test_renames_energy_assistant_db(self, tmp_path: Path) -> None:
        (tmp_path / "energy-assistant.db").write_text("db")
        (tmp_path / "energy-assistant.db-wal").write_text("wal")
        db = tmp_path / "nornwatt.db"

        app_main._adopt_legacy_db(db)

        assert db.read_text() == "db"
        assert (tmp_path / "nornwatt.db-wal").read_text() == "wal"
        assert not (tmp_path / "energy-assistant.db").exists()

    def test_keeps_existing_db(self, tmp_path: Path) -> None:
        (tmp_path / "energy-assistant.db").write_text("old")
        db = tmp_path / "nornwatt.db"
        db.write_text("new")

        app_main._adopt_legacy_db(db)

        assert db.read_text() == "new"
        assert (tmp_path / "energy-assistant.db").exists()

    def test_ignores_other_db_names(self, tmp_path: Path) -> None:
        (tmp_path / "energy-assistant.db").write_text("old")

        app_main._adopt_legacy_db(tmp_path / "history.db")

        assert not (tmp_path / "history.db").exists()
