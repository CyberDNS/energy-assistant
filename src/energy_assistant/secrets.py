"""
SecretsManager — resolves ``!secret`` tags in config files.

Lookup order
------------
1. Environment variable ``ENERGY_ASSISTANT_SECRET_<NAME>``
   (name upper-cased, hyphens → underscores).
2. ``secrets.yaml`` in the same directory as ``config.yaml``.

``secrets.yaml`` must be gitignored and never committed.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

_ENV_PREFIX = "ENERGY_ASSISTANT_SECRET_"


class SecretNotFoundError(KeyError):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"Secret '{name}' not found.  "
            f"Define it in secrets.yaml or set the environment variable "
            f"{_ENV_PREFIX}{name.upper().replace('-', '_')}."
        )


class SecretsManager:
    """Resolves secret references for plugin configuration.

    Parameters
    ----------
    secrets_path:
        Path to ``secrets.yaml``, e.g. ``config_dir / "secrets.yaml"``.
    """

    def __init__(self, secrets_path: Path | str) -> None:
        self._path = Path(secrets_path)
        self._cache: dict[str, str] | None = None

    def get(self, name: str) -> str:
        """Return the value of *name*, searching env vars then secrets.yaml."""
        env_key = _ENV_PREFIX + name.upper().replace("-", "_")
        value = os.environ.get(env_key)
        if value is not None:
            return value

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

        self._cache = {str(k): str(v) for k, v in data.items()}
        return self._cache

    def reload(self) -> None:
        """Invalidate the cached secrets file, forcing a fresh load."""
        self._cache = None
