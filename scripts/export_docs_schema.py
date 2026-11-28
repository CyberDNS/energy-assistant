"""Export the configuration schema for the documentation site.

    uv run python scripts/export_docs_schema.py > schema.json

The output is what ``GET /api/config/schema`` returns (every device type,
preset, tariff, forecast and setting with its label, description and default),
plus the Nornwatt version.  ``.github/workflows/sync-docs.yml`` sends it to
CyberDNS/nornwatt.com, where the settings tables are built from it and the
pages are checked against it.
"""

from __future__ import annotations

import json
import sys
from importlib.metadata import version

from nornwatt.server.config_api import config_schema


def main() -> None:
    json.dump({"version": version("nornwatt"), **config_schema()}, sys.stdout,
              indent=1, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
