"""python -m energy_assistant — launch the Energy Assistant application.

Usage
-----
::

    python -m energy_assistant                     # uses ./config.yaml
    python -m energy_assistant path/to/config.yaml
    python -m energy_assistant config.yaml --db data/history.db

Environment
-----------
``LOG_LEVEL``
    Override the root log level (default: ``INFO``).
    Example: ``LOG_LEVEL=DEBUG python -m energy_assistant``
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .server import Application

_DEFAULT_CONFIG = "config.yaml"
_DEFAULT_DB = "data/history.db"
_HA_CONFIG = Path("/config/energy-assistant/config.yaml")
_HA_DB = Path("/config/energy-assistant/energy-assistant.db")
_LEGACY_CONTAINER_CONFIG = Path("/config/config.yaml")
_LEGACY_CONTAINER_DB = Path("/data/history.db")


def _default_config_path() -> Path:
    env_value = os.environ.get("ENERGY_ASSISTANT_CONFIG")
    if env_value:
        return Path(env_value)
    if _HA_CONFIG.parent.exists():
        return _HA_CONFIG
    if _LEGACY_CONTAINER_CONFIG.exists():
        return _LEGACY_CONTAINER_CONFIG
    return Path(_DEFAULT_CONFIG)


def _default_db_path() -> Path:
    env_value = os.environ.get("ENERGY_ASSISTANT_DB")
    if env_value:
        return Path(env_value)
    if _HA_DB.parent.exists():
        return _HA_DB
    if _LEGACY_CONTAINER_DB.parent.exists():
        return _LEGACY_CONTAINER_DB
    return Path(_DEFAULT_DB)


def _parse_args() -> tuple[Path, Path]:
    """Return (config_path, db_path) from ``sys.argv``."""
    args = sys.argv[1:]
    config_path: Path | None = None
    db_path: Path | None = None
    idx = 0
    while idx < len(args):
        arg = args[idx]
        if arg in {"--config", "-c"} and idx + 1 < len(args):
            config_path = Path(args[idx + 1])
            idx += 2
            continue
        if arg == "--db" and idx + 1 < len(args):
            db_path = Path(args[idx + 1])
            idx += 2
            continue
        if not arg.startswith("-") and config_path is None:
            config_path = Path(arg)
        idx += 1

    if config_path is None:
        config_path = _default_config_path()
    if db_path is None:
        db_path = _default_db_path()
    return config_path, db_path


def _configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


async def _run(config_path: Path, db_path: Path) -> None:
    app = Application(config_path=config_path, db_path=db_path)
    loop = asyncio.get_running_loop()

    def _signal_handler(sig: signal.Signals) -> None:
        logging.getLogger(__name__).info("Received %s — shutting down", sig.name)
        for task in asyncio.all_tasks(loop):
            task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, sig)

    await app.run_forever()


def main() -> None:
    _configure_logging()
    config_path, db_path = _parse_args()
    asyncio.run(_run(config_path, db_path))


if __name__ == "__main__":
    main()
