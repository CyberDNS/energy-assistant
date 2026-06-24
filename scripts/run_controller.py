"""VS Code dev launcher for Energy Assistant.

Usage (via launch.json):
    python scripts/run_controller.py              # live mode, local config + temp DB
    python scripts/run_controller.py --dry-run    # same, but no commands sent to devices
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the package importable when running directly (not installed via uv).
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
os.chdir(ROOT)

# --dry-run: prevent any commands being sent to real devices.
if "--dry-run" in sys.argv:
    sys.argv.remove("--dry-run")
    os.environ["ENERGY_ASSISTANT_DRY_RUN"] = "1"

# Use a dedicated dev port so this never conflicts with a running HA addon.
os.environ.setdefault("ENERGY_ASSISTANT_PORT", "8089")

# Default to local config.yaml and a throwaway DB so production data is untouched.
has_positional = any(a for a in sys.argv[1:] if not a.startswith("-"))
if not has_positional:
    sys.argv += ["config.yaml", "--db", "/tmp/ea-dev.db"]

from energy_assistant.__main__ import main  # noqa: E402

main()
