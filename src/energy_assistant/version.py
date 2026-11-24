"""The running Energy Assistant's version, for the UI, logs and problem reports.

The release version comes from the installed package (``pyproject.toml``).
Container images also carry the build they were made from, baked in by
``docker-publish.yml`` as environment variables:

``ENERGY_ASSISTANT_COMMIT``
    The git commit the image was built from.
``ENERGY_ASSISTANT_CHANNEL``
    ``dev`` for builds of ``main`` (newer than the last release, so the
    version alone doesn't identify them); empty for release tags.

A dev build shows as ``0.1.3-dev+1a2b3c4``, a release as ``0.1.3``.
"""

from __future__ import annotations

import os
from functools import cache
from importlib.metadata import PackageNotFoundError, version

_PACKAGE = "energy-assistant"


@cache
def version_info() -> dict[str, str | None]:
    """``{"version", "release", "channel", "commit"}`` of the running build."""
    try:
        release = version(_PACKAGE)
    except PackageNotFoundError:  # running from a source tree without install
        release = "0.0.0"
    commit = os.environ.get("ENERGY_ASSISTANT_COMMIT", "").strip()[:7] or None
    channel = os.environ.get("ENERGY_ASSISTANT_CHANNEL", "").strip().lower() or None
    label = release
    if channel:
        label += f"-{channel}"
    if channel and commit:
        label += f"+{commit}"
    return {"version": label, "release": release, "channel": channel, "commit": commit}


def version_label() -> str:
    """The version as shown to users, e.g. ``0.1.3`` or ``0.1.3-dev+1a2b3c4``."""
    return version_info()["version"]  # type: ignore[return-value]
