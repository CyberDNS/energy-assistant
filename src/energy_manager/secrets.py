"""
SecretsManager — secure credential resolution for plugin configuration.

Secrets are referenced in ``config.yaml`` using the ``!secret`` YAML tag:

    devices:
      - id: solar_inverter
        plugin: energy_manager.plugins.iobroker
        data:
          host: 192.168.1.5
          api_token: !secret iobroker_api_token

The actual values are resolved from two sources, in priority order:

1. **Environment variables** — ``ENERGY_ASSISTANT_SECRET_<NAME>`` (upper-cased,
   hyphens replaced with underscores).  Useful in Docker / CI environments where
   injecting a secrets file is inconvenient.

   Example::

       ENERGY_ASSISTANT_SECRET_IOBROKER_API_TOKEN=abc123

2. **``secrets.yaml``** — a file in the same directory as ``config.yaml``.
   This file must be gitignored and should have permissions 600.

   Example ``secrets.yaml``::

       iobroker_api_token: "abc123"
       tibber_token: "eyJhbGciOi..."

Security notes
--------------
- Secret values are **never** written to logs.
- ``SecretNotFoundError`` messages include the secret name but never the value.
- ``secrets.yaml`` is excluded from version control via ``.gitignore``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml


_ENV_PREFIX = "ENERGY_ASSISTANT_SECRET_"


class SecretNotFoundError(KeyError):
    """Raised when a referenced secret cannot be resolved from any source."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"Secret '{name}' not found. "
            f"Define it in secrets.yaml or set the environment variable "
            f"{_ENV_PREFIX}{name.upper().replace('-', '_')}."
        )


class SecretsManager:
    """
    Resolves secret values from environment variables and ``secrets.yaml``.

    Parameters
    ----------
    secrets_path:
        Path to the ``secrets.yaml`` file.  The file need not exist — if it
        is absent, only environment variable resolution is attempted.
    """

    def __init__(self, secrets_path: Path | str) -> None:
        self._path = Path(secrets_path)
        self._cache: dict[str, str] | None = None

    def get(self, name: str) -> str:
        """
        Return the secret value for *name*.

        Raises ``SecretNotFoundError`` if the secret is not found in either
        the environment or ``secrets.yaml``.
        """
        # 1. Environment variable takes priority.
        env_key = _ENV_PREFIX + name.upper().replace("-", "_")
        value = os.environ.get(env_key)
        if value is not None:
            return value

        # 2. secrets.yaml (loaded once and cached).
        file_secrets = self._load_file()
        if name in file_secrets:
            return file_secrets[name]

        raise SecretNotFoundError(name)

    def _load_file(self) -> dict[str, str]:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        with self._path.open() as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ValueError(
                f"secrets.yaml must contain a YAML mapping at the top level, "
                f"got {type(data).__name__}."
            )
        # Stringify all values — secrets are always treated as strings.
        self._cache = {str(k): str(v) for k, v in data.items()}
        return self._cache

    def reload(self) -> None:
        """Invalidate the in-memory cache so the next ``get()`` re-reads the file."""
        self._cache = None
