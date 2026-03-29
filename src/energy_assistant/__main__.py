"""python -m energy_assistant — launch the Energy Assistant application.

Usage
-----
::

    python -m energy_assistant                     # uses ./config.yaml (local)
    python -m energy_assistant path/to/config.yaml
    python -m energy_assistant config.yaml --db data/history.db

Runtime modes
-------------
The app detects which mode it is running in and chooses default paths:

* **Local / VS Code** — ``./config.yaml`` and ``./data/history.db``
* **Home Assistant add-on** — ``/config/config.yaml`` and
  ``/data/energy-assistant.db``

Detection order:

1. ``ENERGY_ASSISTANT_MODE`` env var — explicit override
   (``ha`` / ``local`` / ``dev`` etc.)
2. Presence of ``/data/options.json`` — written by the Supervisor only
   inside a running HA add-on container.

Both paths can be overridden individually regardless of mode:

* ``ENERGY_ASSISTANT_CONFIG`` — full path to the YAML config file
* ``ENERGY_ASSISTANT_DB`` — full path to the SQLite database file

Home Assistant filesystem mapping
----------------------------------
Inside the add-on container the Supervisor mounts:

* ``/data``   — private persistent storage (always available, writable).
  ``/data/options.json`` contains the user-configured options.
* ``/config`` — user-accessible folder, mounted from the host at
  ``/addon_configs/{REPO}_{slug}/``.  Users browse this via File Editor.
  ``{REPO}`` is ``local`` for local installs or a hash of the GitHub
  repo URL for store installs.  ``{slug}`` is defined in the add-on
  repository's ``config.yaml``.

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
# Home Assistant add-on paths (inside the container).
# /config  → host: /addon_configs/{REPO}_{slug}/  (user-visible via File Editor)
# /data    → host: managed by Supervisor, private persistent storage
_HA_CONFIG = Path("/config/config.yaml")
_HA_DB = Path("/data/energy-assistant.db")


def _is_home_assistant_runtime() -> bool:
    """Return True when running as a Home Assistant add-on container."""
    mode = os.environ.get("ENERGY_ASSISTANT_MODE", "").strip().lower()
    if mode in {"ha", "homeassistant", "home-assistant", "addon", "add-on"}:
        return True
    if mode in {"local", "dev", "development"}:
        return False
    return Path("/data/options.json").exists()


def _default_config_path() -> Path:
    env_value = os.environ.get("ENERGY_ASSISTANT_CONFIG")
    if env_value:
        return Path(env_value)
    if _is_home_assistant_runtime():
        return _HA_CONFIG
    return Path(_DEFAULT_CONFIG)


def _default_db_path() -> Path:
    env_value = os.environ.get("ENERGY_ASSISTANT_DB")
    if env_value:
        return Path(env_value)
    if _is_home_assistant_runtime():
        return _HA_DB
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
