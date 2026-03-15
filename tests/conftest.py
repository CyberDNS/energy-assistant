"""
Shared pytest fixtures.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


@pytest.fixture
def tmp_yaml(tmp_path: Path) -> Path:
    return tmp_path / "config.yaml"
