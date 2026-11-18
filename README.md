# Energy Assistant

An open-source, vendor-neutral energy management platform for homeowners.

See [VISION.md](VISION.md) for the project vision and [ARCHITECTURE.md](ARCHITECTURE.md) for the technical design.

## Container images

Pre-built multi-arch images are published to GHCR:

```
ghcr.io/cyberdns/energy-assistant:dev      # latest main branch (dev channel)
ghcr.io/cyberdns/energy-assistant:edge     # latest tagged version (edge channel)
ghcr.io/cyberdns/energy-assistant:latest   # last version promoted to prod
ghcr.io/cyberdns/energy-assistant:<tag>    # pinned to a specific release tag
```

See [RELEASE.md](RELEASE.md) for the full release model, versioning strategy,
and release notes approach.

## Home Assistant add-on

Energy Assistant is available as a Home Assistant add-on via
[CyberDNS/hassio-addons-repository](https://github.com/CyberDNS/hassio-addons-repository).
Three channels are available:

| Add-on                        | Stability        |
|-------------------------------|------------------|
| Energy Assistant (dev)        | Nightly / unstable |
| Energy Assistant (edge)       | Pre-release       |
| Energy Assistant              | Stable            |

Recommended file locations in Home Assistant:

- Config file (user-editable): `/config/config.yaml`
- SQLite DB (private, persistent): `/data/energy-assistant.db`

If you want users to inspect/back up the DB via File Editor, set
`ENERGY_ASSISTANT_DB=/config/energy-assistant.db`
and map `addon_config` with write access in the add-on definition.

The app supports overriding both paths via environment variables:

- `ENERGY_ASSISTANT_CONFIG`
- `ENERGY_ASSISTANT_DB`

Two runtime use cases are supported by default:

| Use case | Runtime hint | Default config path | Default DB path |
|---|---|---|---|
| Local development (VS Code) | `ENERGY_ASSISTANT_MODE=local` (optional) | `./config.yaml` | `./data/history.db` |
| Home Assistant add-on | `ENERGY_ASSISTANT_MODE=ha` (optional) or auto-detected via `/data/options.json` | `/config/config.yaml` | `/data/energy-assistant.db` |

### Remote read-only access (REST + MCP)

Through ingress the API needs no login. To read it from elsewhere — scripts,
or an AI assistant debugging the live installation over MCP — give it a token
and publish the port:

1. Set a token in `config.yaml` (generate one with `openssl rand -hex 32`):

   ```yaml
   server:
     api_token: !secret energy_assistant_api_token
   ```

   or set `ENERGY_ASSISTANT_API_TOKEN` in the environment.
2. In the add-on's **Network** settings, map container port `8088/tcp` to a
   host port (it is unmapped by default), then restart the add-on.

With a token set, every request that does not come through ingress (or from
localhost) must send `Authorization: Bearer <token>`, and only gets read
access: `GET` on the REST API and the MCP endpoint. Writes are refused (403);
the web UI behind ingress keeps full control. `/health` stays open.

The MCP endpoint is `http://<host>:<port>/mcp` (streamable HTTP, stateless).
Its tools are read-only wrappers around the REST API: `get_status`,
`get_plan` (full per-slot flows), `get_forecast`, `get_savings`,
`get_savings_slots` (real vs. simulated uncontrolled house per accounting
slot, with all cost components), `get_history`, `get_ledger`, `get_config`, `get_ev`,
`get_ev_plan`, `get_controllable`, `get_learned_forecasts`, `get_plan_inputs`.
For Claude Code:

```bash
claude mcp add --transport http energy-assistant http://<host>:<port>/mcp \
  --header "Authorization: Bearer <token>"
```

### Embedding the UI in a dashboard

The Plan tab can be deep-linked and embedded (e.g. in an iframe / webpage card):

| URL | Shows |
|---|---|
| `#/plan` | Plan tab |
| `#/savings` | Savings tab: savings against an uncontrolled house over rolling windows (24 h – 1 y) |
| `#/plan/<vehicle>` or `#/plan?vehicle=<vehicle>` | One EV only: target, 7-day strip, weekly plan, "Charge now" and its charging-plan chart. `<vehicle>` is the EV's asset id (case-insensitive). |
| `…?embed=1` | Hides the header, KPI tiles and tab bar. Combine with any route, e.g. `#/plan/banzert?embed=1`. |

Hosts that drop the URL fragment (the Home Assistant ingress panel does) can
pass the same options as a query string on the page URL instead:
`?view=plan`, `?view=savings`, `?vehicle=<vehicle>`, `?embed=1` — e.g. `/?vehicle=banzert&embed=1`.
When opened without any route, the UI restores the last-used tab.

All frontend libraries (Vue, Vue Router, Plotly) are bundled in
`frontend/vendor/`; the UI makes no external requests.

### Weekly EV plan in Home Assistant

With `backends.mqtt` configured, each EV's weekly plan is exposed through MQTT
discovery as configuration entities on the EV device — per weekday a `number`
(target SoC), a `time` (departure, local time) and a `switch` (day enabled).
Changes are written straight to the weekly plan and trigger a re-plan; edits
made in the Energy Assistant UI are mirrored back to the entities.

### Savings KPIs in Home Assistant

With `backends.mqtt` configured, an "Energy Assistant" device exposes the
cumulative **Real savings** in € (`device_class: monetary`, `state_class:
total`). Use Home Assistant's `utility_meter` or statistics cards to get daily,
monthly or yearly figures.

The saving compares the real house with a **simulated uncontrolled house** on
the same measured load, PV and prices, per 15-minute slot:

- its batteries charge from PV surplus only and discharge to cover the load of
  the zone they regulate on (household and cars, not a separately billed heat
  pump),
- a car charges from PV surplus left after the batteries, and from the grid as
  late as possible to reach its charge plan's target by the deadline.

Both houses keep a ledger per battery: charging moves the energy's cost into
the ledger (grid energy at the import price, PV at the feed-in price it would
have earned) and using it costs the ledger's average cost. So a charging slot
saves nothing and the saving shows when stored energy replaces grid power:
(price at that moment − ledger cost) × kWh. Car energy charged earlier than
the uncontrolled car would have is held at its cost until the uncontrolled car
charges it too. The Savings tab and `GET /api/savings?hours=168` show the
same numbers; for the 24 h and 3 d ranges the tab also charts, per 15-minute
slot, the stored energy and battery/car power of both houses next to the
price. Details in `src/energy_assistant/core/savings.py`.

Threshold devices count as fixed load and contribute no saving. Nothing is
published in dry-run mode.

### Price curve in Home Assistant

With `backends.mqtt` configured, the "Energy Assistant" device also exposes:

| Sensor | Meaning |
|---|---|
| `sensor.energy_assistant_electricity_price` | Current import price (€/kWh); its attributes hold the whole curve |
| `sensor.energy_assistant_effective_electricity_price` | What one extra kWh costs right now, given the PV and consumption forecasts |

The effective price is the import price while the site imports, the feed-in
price while PV surplus is forecast (using the energy loses that income), and
a blend of both in between (`share = (load − pv) / load`). It is a display
figure; the optimizer plans against the raw prices. Details:
`src/energy_assistant/core/price_curve.py`.

The curve attribute `data` has one row per slot,
`[epoch_ms, price, effective, feed_in, estimated]`, from the start of the
last 24 hours to the end of the plan (refreshed after every plan).
`estimated = 1` marks prices the tariff has not published yet (repeated from
the previous day). An [ApexCharts card](https://github.com/RomRider/apexcharts-card):

```yaml
type: custom:apexcharts-card
header:
  show: true
  title: Electricity prices
graph_span: 2d
span:
  start: day
now:
  show: true
  label: now
apex_config:
  legend:
    show: true
  yaxis:
    - decimalsInFloat: 3
series:
  - entity: sensor.energy_assistant_electricity_price
    name: Import
    color: '#e07070'
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[4] ? null : r[1]]);
  - entity: sensor.energy_assistant_electricity_price
    name: Import (estimated)
    color: '#e07070'
    stroke_dash: 4
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[4] ? r[1] : null]);
  - entity: sensor.energy_assistant_electricity_price
    name: Effective
    color: orange
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[2]]);
  - entity: sensor.energy_assistant_electricity_price
    name: Feed-in
    color: '#4caf7d'
    stroke_dash: 6
    curve: stepline
    extend_to: false
    data_generator: |
      return (entity.attributes.data || []).map(r => [r[0], r[3]]);
```
