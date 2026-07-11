"""Audit the active plan for EV-PV consistency, slot by slot.

Fetches /api/plan + /api/forecast from a running server and, for every slot
with EV charging, prints the forecast PV, consumption, battery and EV powers,
the derived grid import (same formula as the UI flow graph), and flags any
slot where a charge_from_pv EV intent exceeds the forecast PV surplus
(which the MILP constraint should make impossible).

Usage:
    python scripts/audit_plan.py                          # http://localhost:8088
    python scripts/audit_plan.py --url http://host:8088
"""

from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import datetime


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.load(r)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:8088")
    ap.add_argument("--all", action="store_true", help="print all slots, not just EV ones")
    args = ap.parse_args()

    plan = fetch(f"{args.url}/api/plan")
    forecast = fetch(f"{args.url}/api/forecast")
    ev_info = fetch(f"{args.url}/api/ev")
    ev_ids = {e["device_id"] for e in ev_info}

    ts_index = {ts: i for i, ts in enumerate(forecast["timestamps"])}
    pv = list(forecast["pv_kw"])
    cons = forecast["consumption_kw"]

    # Prefer the solver's effective PV (includes the live-PV floor for the
    # current hour) and its solved grid import when the plan provides them.
    solved_import: dict[str, float] = {}
    for f in plan.get("flows", []):
        i = ts_index.get(f["timestep"])
        if i is not None:
            pv[i] = f["pv_kw"]
        solved_import[f["timestep"]] = f["grid_import_kw"]

    by_ts: dict[str, list[dict]] = {}
    for intent in plan.get("intents", []):
        by_ts.setdefault(intent["timestep"], []).append(intent)

    print(f"plan created: {plan.get('created_at')}  step: {plan.get('step_minutes')} min")
    print(f"{'time':16}  {'pv':>6} {'cons':>6} {'bat':>7} {'ev':>6} {'evmode':>15} {'other':>6}  {'import':>7}")

    violations = 0
    for ts in sorted(by_ts):
        i = ts_index.get(ts)
        if i is None:
            continue
        slots = by_ts[ts]
        evs = [s for s in slots if s["device_id"] in ev_ids and s["planned_kw"] > 0]
        if not evs and not args.all:
            continue

        bat_kw = sum(s["planned_kw"] for s in slots
                     if s["device_id"] not in ev_ids and s.get("mode") not in ("run", "standby"))
        other_kw = sum(s["planned_kw"] for s in slots if s.get("mode") in ("run", "standby") and s["planned_kw"] > 0)
        ev_kw = sum(s["planned_kw"] for s in evs)
        ev_modes = ",".join(s.get("mode", "?") for s in evs)

        imp = solved_import.get(ts)
        if imp is None:
            imp = max(0.0, cons[i] + max(bat_kw, 0) + ev_kw + other_kw - pv[i] - max(-bat_kw, 0))
        surplus = max(0.0, pv[i] - cons[i])

        flag = ""
        for s in evs:
            if s.get("mode") == "charge_from_pv" and s["planned_kw"] > surplus + 0.05:
                flag = f"  ← VIOLATION: pv-labeled {s['planned_kw']:.2f} kW > surplus {surplus:.2f} kW"
                violations += 1

        local = datetime.fromisoformat(ts).astimezone().strftime("%d.%m %H:%M")
        print(f"{local:16}  {pv[i]:6.2f} {cons[i]:6.2f} {bat_kw:7.2f} {ev_kw:6.2f} {ev_modes:>15} {other_kw:6.2f}  {imp:7.2f}{flag}")

    print(f"\npv-surplus violations: {violations}")
    if violations == 0:
        print("(any grid import shown above is attributable to consumption/battery, not the pv-labeled EV energy)")


if __name__ == "__main__":
    main()
