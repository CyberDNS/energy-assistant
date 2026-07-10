"""Quick inspection of the running API to debug EV plan intent fields.

Usage:
    python inspect_api.py [http://host:port]

Defaults to http://localhost:8088
"""
import json
import sys
import urllib.request
from datetime import datetime, timezone

BASE = sys.argv[1].rstrip('/') if len(sys.argv) > 1 else 'http://localhost:8088'


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.loads(r.read())


# ── /api/ev ─────────────────────────────────────────────────────────────────
print("=" * 70)
print("EV goals (/api/ev)")
print("=" * 70)
try:
    evs = get('/api/ev')
    phase2_by_device = {}
    for ev in evs:
        g = ev.get('goal')
        phase2_by_device[ev['device_id']] = g['phase2_start'] if g else None
        print(f"  {ev['asset_id']} ({ev['device_id']})")
        print(f"    connected={ev['connected']}  soc={ev.get('soc_pct')}%")
        if g:
            print(f"    target={g['target_soc_pct']}% by {g['target_by']}")
            print(f"    phase1={g['phase1_kwh']} kWh  phase2={g['phase2_kwh']} kWh")
            print(f"    phase2_start={g['phase2_start']}")
        else:
            print("    no goal")
except Exception as e:
    print(f"  ERROR: {e}")
    evs = []
    phase2_by_device = {}

# ── /api/forecast prices aligned with EV intents ─────────────────────────────
print()
print("=" * 70)
print("Prices + EV intents (aligned by timestamp)")
print("=" * 70)
ev_device_ids = {ev['device_id'] for ev in evs}
try:
    plan     = get('/api/plan')
    forecast = get('/api/forecast')

    ts_list   = forecast.get('timestamps', [])
    prices    = forecast.get('variable_prices') or forecast.get('prices', [])
    pv_kw     = forecast.get('pv_kw', [])
    price_map = {ts: (prices[i] if i < len(prices) else None) for i, ts in enumerate(ts_list)}
    pv_map    = {ts: (pv_kw[i]  if i < len(pv_kw)  else None) for i, ts in enumerate(ts_list)}

    intents = plan.get('intents', [])
    ev_intents = sorted(
        [i for i in intents if i['device_id'] in ev_device_ids],
        key=lambda x: (x['device_id'], x['timestep'])
    )

    if not ev_intents:
        print("  No EV intents in plan — checking all plan timestamps for context...")
        # Print price table around now for context
        now_iso = datetime.now(timezone.utc).isoformat()
        for i, ts in enumerate(ts_list):
            if ts >= now_iso:
                for j in range(max(0, i-2), min(len(ts_list), i+20)):
                    t = ts_list[j]
                    p = prices[j] if j < len(prices) else '?'
                    pv = pv_kw[j] if j < len(pv_kw) else '?'
                    marker = ' ← now' if j == i else ''
                    print(f"  {t}  price={p:.4f} €/kWh  pv={pv:.2f} kW{marker}")
                break
    else:
        print(f"  {'timestamp':<26} {'device':<28} {'mode':<18} {'kW':>5} {'phase':>6}  {'price €/kWh':>12}  {'PV kW':>7}")
        print(f"  {'-'*26} {'-'*28} {'-'*18} {'-'*5} {'-'*6}  {'-'*12}  {'-'*7}")
        prev_ts = None
        for i in ev_intents:
            ts  = i['timestep']
            p2  = phase2_by_device.get(i['device_id'])
            if not i.get('grid_allowed', True):
                phase = 'PV'
            elif p2 and ts >= p2:
                phase = 'phase2'
            else:
                phase = 'phase1'
            price = price_map.get(ts)
            pv    = pv_map.get(ts)
            gap   = '  ---' if prev_ts and ts > prev_ts else ''
            if gap:
                print(gap)
            print(f"  {ts:<26} {i['device_id']:<28} {i['mode']:<18} {i.get('planned_kw', 0):>5.2f} {phase:>6}  "
                  f"{(f'{price:.4f}' if price is not None else '?'):>12}  "
                  f"{(f'{pv:.2f}' if pv is not None else '?'):>7}")
            prev_ts = ts

    # Also print the full price table for context (cheapest hours highlighted)
    print()
    print("=" * 70)
    print("Full price schedule (sorted cheapest first)")
    print("=" * 70)
    ev_ts_set = {i['timestep'] for i in ev_intents}
    now_iso = datetime.now(timezone.utc).isoformat()
    priced = [(ts_list[i], prices[i], pv_kw[i] if i < len(pv_kw) else 0)
              for i in range(min(len(ts_list), len(prices)))]
    priced_future = [(ts, p, pv) for ts, p, pv in priced if ts >= now_iso]
    for ts, p, pv in sorted(priced_future, key=lambda x: x[1])[:20]:
        marker = ' ◄ EV' if ts in ev_ts_set else ''
        print(f"  {ts}  {p:.4f} €/kWh  pv={pv:.2f} kW{marker}")

except Exception as e:
    import traceback; traceback.print_exc()
    print(f"  ERROR: {e}")
