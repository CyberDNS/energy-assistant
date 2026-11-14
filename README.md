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

### Embedding the UI in a dashboard

The Plan tab can be deep-linked and embedded (e.g. in an iframe / webpage card):

| URL | Shows |
|---|---|
| `#/plan` | Plan tab |
| `#/plan/<vehicle>` or `#/plan?vehicle=<vehicle>` | One EV only: target, 7-day strip, weekly plan, "Charge now" and its charging-plan chart. `<vehicle>` is the EV's asset id (case-insensitive). |
| `…?embed=1` | Hides the header, KPI tiles and tab bar. Combine with any route, e.g. `#/plan/banzert?embed=1`. |

Hosts that drop the URL fragment (the Home Assistant ingress panel does) can
pass the same options as a query string on the page URL instead:
`?view=plan`, `?vehicle=<vehicle>`, `?embed=1` — e.g. `/?vehicle=banzert&embed=1`.
When opened without any route, the UI restores the last-used tab.

All frontend libraries (Vue, Vue Router, Plotly) are bundled in
`frontend/vendor/`; the UI makes no external requests.

### Weekly EV plan in Home Assistant

With `backends.mqtt` configured, each EV's weekly plan is exposed through MQTT
discovery as configuration entities on the EV device — per weekday a `number`
(target SoC), a `time` (departure, local time) and a `switch` (day enabled).
Changes are written straight to the weekly plan and trigger a re-plan; edits
made in the Energy Assistant UI are mirrored back to the entities.
