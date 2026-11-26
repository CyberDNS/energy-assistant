"""python -m nornwatt — launch the Nornwatt application.

Usage
-----
::

    python -m nornwatt                     # uses ./config.yaml (local)
    python -m nornwatt path/to/config.yaml
    python -m nornwatt config.yaml --db data/history.db

Runtime modes
-------------
The app detects which mode it is running in and chooses default paths:

* **Local / VS Code** — ``./config.yaml`` and ``./data/history.db``
* **Home Assistant add-on** — ``/config/config.yaml`` and
  ``/data/nornwatt.db``

Detection order:

1. ``NORNWATT_MODE`` env var — explicit override
   (``ha`` / ``local`` / ``dev`` etc.)
2. Presence of ``/data/options.json`` — written by the Supervisor only
   inside a running HA add-on container.

Both paths can be overridden individually regardless of mode:

* ``NORNWATT_CONFIG`` — full path to the YAML config file
* ``NORNWATT_DB`` — full path to the SQLite database file

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
    Example: ``LOG_LEVEL=DEBUG python -m nornwatt``
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from .server import Application
from .version import version_label

_DEFAULT_CONFIG = "config.yaml"
_DEFAULT_DB = "data/history.db"
# Home Assistant add-on paths (inside the container).
# /config  → host: /addon_configs/{REPO}_{slug}/  (user-visible via File Editor)
# /data    → host: managed by Supervisor, private persistent storage
_HA_CONFIG = Path("/config/config.yaml")
_HA_DB = Path("/data/nornwatt.db")
# The database file of the add-on under its former name, Energy Assistant.
_LEGACY_DB_NAME = "energy-assistant.db"


def _is_home_assistant_runtime() -> bool:
    """Return True when running as a Home Assistant add-on container."""
    mode = os.environ.get("NORNWATT_MODE", "").strip().lower()
    if mode in {"ha", "homeassistant", "home-assistant", "addon", "add-on"}:
        return True
    if mode in {"local", "dev", "development"}:
        return False
    return Path("/data/options.json").exists()


def _default_config_path() -> Path:
    env_value = os.environ.get("NORNWATT_CONFIG")
    if env_value:
        return Path(env_value)
    if _is_home_assistant_runtime():
        return _HA_CONFIG
    return Path(_DEFAULT_CONFIG)


def _default_db_path() -> Path:
    env_value = os.environ.get("NORNWATT_DB")
    if env_value:
        return Path(env_value)
    if _is_home_assistant_runtime():
        return _HA_DB
    return Path(_DEFAULT_DB)


def _adopt_legacy_db(db_path: Path) -> None:
    """Rename a database left by the former name (Energy Assistant) to *db_path*.

    Only for the add-on's default file: copying ``energy-assistant.db`` from the
    old add-on's ``/data`` into the new one is then enough to keep the history.
    """
    if db_path.name != _HA_DB.name or db_path.exists():
        return
    legacy = db_path.with_name(_LEGACY_DB_NAME)
    if not legacy.exists():
        return
    for suffix in ("", "-wal", "-shm"):
        old = legacy.with_name(legacy.name + suffix)
        if old.exists():
            old.rename(db_path.with_name(db_path.name + suffix))
    logging.getLogger(__name__).info("Database %s renamed to %s", legacy, db_path)


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
    main_task = asyncio.current_task()
    assert main_task is not None

    def _signal_handler(sig: signal.Signals) -> None:
        # Only the main task: ``run_forever`` then stops the loops and
        # releases every device while the device connections (MQTT bridges,
        # the Home Assistant websocket) still run.  Cancelling all tasks here
        # would close them first and no release could be sent.
        logging.getLogger(__name__).info("Received %s — shutting down", sig.name)
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler, sig)

    await app.run_forever()


_HELP = """\
Usage: python -m nornwatt [config.yaml] [--db path/to/history.db]

Options:
  -c, --config FILE   Path to config YAML (default: ./config.yaml or /config/config.yaml in HA)
  --db FILE           Path to SQLite database (default: ./data/history.db or /data/nornwatt.db in HA)
  -h, --help          Show this message and exit
"""


def main() -> None:
    if any(a in sys.argv[1:] for a in ("-h", "--help")):
        print(_HELP, end="")
        return
    _configure_logging()
    logging.getLogger(__name__).info("Nornwatt %s", version_label())
    config_path, db_path = _parse_args()
    _adopt_legacy_db(db_path)
    # A config mistake never ends the process: the app then starts in
    # "configuration needed" state and shows the problems in the web UI.
    asyncio.run(_run(config_path, db_path))


if __name__ == "__main__":
    main()
