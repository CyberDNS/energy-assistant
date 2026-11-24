"""Version label of the running build (UI, logs, problem reports)."""

from importlib.metadata import version

import pytest

from energy_assistant import version as version_mod


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv("ENERGY_ASSISTANT_COMMIT", raising=False)
    monkeypatch.delenv("ENERGY_ASSISTANT_CHANNEL", raising=False)
    version_mod.version_info.cache_clear()
    yield
    version_mod.version_info.cache_clear()


RELEASE = version("energy-assistant")


def test_release_build_shows_plain_version(monkeypatch):
    monkeypatch.setenv("ENERGY_ASSISTANT_COMMIT", "1a2b3c4d5e6f")
    assert version_mod.version_info() == {
        "version": RELEASE, "release": RELEASE, "channel": None, "commit": "1a2b3c4",
    }


def test_dev_build_shows_channel_and_short_commit(monkeypatch):
    monkeypatch.setenv("ENERGY_ASSISTANT_COMMIT", "1a2b3c4d5e6f")
    monkeypatch.setenv("ENERGY_ASSISTANT_CHANNEL", "dev")
    assert version_mod.version_label() == f"{RELEASE}-dev+1a2b3c4"


def test_source_run_without_build_info():
    assert version_mod.version_info() == {
        "version": RELEASE, "release": RELEASE, "channel": None, "commit": None,
    }
