# Installation

How to run Nornwatt: as a Home Assistant add-on or as a container.

## Home Assistant add-on

Nornwatt is available as a Home Assistant add-on via
[CyberDNS/hassio-addons-repository](https://github.com/CyberDNS/hassio-addons-repository).
Three channels are available:

| Add-on | Stability |
|---|---|
| Nornwatt (dev) | Nightly / unstable |
| Nornwatt (edge) | Pre-release |
| Nornwatt | Stable |

## First start

1. Install the add-on and start it. No `config.yaml` and no access token are
   needed.
2. Open **Nornwatt** in the sidebar. It is connected to the Home
   Assistant it runs in already (shown as "Home Assistant (this
   installation)" on the start page and under Settings → Connections) and
   opens on a start page with the first steps.
3. **Add a device** (start with the meter at the grid connection), then your
   tariff. Each one is applied as soon as it is saved; the plan follows within
   seconds.

How the add-on reaches its Home Assistant: through the Supervisor
(`http://supervisor/core/api`, websocket `ws://supervisor/core/websocket`)
with the add-on's own `SUPERVISOR_TOKEN` (the add-on has
`homeassistant_api: true`). A `backends.homeassistant` in `config.yaml`
(URL + long-lived token, e.g. a Home Assistant on another machine) takes its
place. Outside the add-on (`SUPERVISOR_TOKEN` not set) only a configured
`backends.homeassistant` is used.

**A mistake in the configuration never stops Nornwatt.** It starts
anyway, in *configuration needed* state: the web UI, the settings and the
Home Assistant connection work, nothing is planned or controlled. A red notice
on every page lists every problem (an unknown setting, a missing secret, a
YAML syntax error with its line and column, …) with a way to fix it:

- problems in a device, tariff or section: fix them in their Settings page.
  While the file has problems, a change is saved even before everything is
  valid, as long as it adds no new problem, so they can be fixed one by one;
- a missing secret: set it under Settings → Secrets;
- a file that is not valid YAML: Settings → **config.yaml** edits the file as
  text and shows the line of the error.

As soon as the file is valid it is applied, without a restart, and planning
starts. A configuration that runs is never replaced from the UI by one with
problems: such a change is refused and listed in the form. A device that
cannot be reached at the start is shown as not responding; everything else
runs.

`GET /api/status` (and `/health`) report the state as `configuration`, and
`GET /api/config/validate` lists the problems of the file as it is on disk.

Recommended file locations in Home Assistant:

- Config file (user-editable): `/config/config.yaml`
- SQLite DB (private, persistent): `/data/nornwatt.db`

If you want users to inspect/back up the DB via File Editor, set
`NORNWATT_DB=/config/nornwatt.db`
and map `addon_config` with write access in the add-on definition.

The app supports overriding both paths via environment variables:

- `NORNWATT_CONFIG`
- `NORNWATT_DB`

Two runtime use cases are supported by default:

| Use case | Runtime hint | Default config path | Default DB path |
|---|---|---|---|
| Local development (VS Code) | `NORNWATT_MODE=local` (optional) | `./config.yaml` | `./data/history.db` |
| Home Assistant add-on | `NORNWATT_MODE=ha` (optional) or auto-detected via `/data/options.json` | `/config/config.yaml` | `/data/nornwatt.db` |

## Moving from Energy Assistant

Nornwatt is the new name of Energy Assistant. Home Assistant sees the renamed
add-on (slug `nornwatt…` instead of `energy-assistant…`) as a new one, so:

1. Stop the old add-on and install the Nornwatt add-on of the same channel.
2. Copy `config.yaml` and `secrets.yaml` from the old add-on's folder in
   `/addon_configs/` to the new one's.
3. To keep the history, copy `energy-assistant.db` from the old add-on's data
   folder into the new one's; Nornwatt renames it to `nornwatt.db` at its first
   start.
4. Start Nornwatt. Its first connection to MQTT removes the old Energy
   Assistant entities from Home Assistant; the new ones are named `…nornwatt…`
   (e.g. `sensor.nornwatt_electricity_price`). Update automations and dashboards.
5. Uninstall the old add-on.

Outside the add-on, environment variables are now named `NORNWATT_…` (formerly
`ENERGY_ASSISTANT_…`) and the command is `nornwatt`.

## Container images

Pre-built multi-arch images are published to GHCR:

```
ghcr.io/cyberdns/nornwatt:dev      # latest main branch (dev channel)
ghcr.io/cyberdns/nornwatt:edge     # latest tagged version (edge channel)
ghcr.io/cyberdns/nornwatt:latest   # last version promoted to prod
ghcr.io/cyberdns/nornwatt:<tag>    # pinned to a specific release tag
```

See [RELEASE.md](../RELEASE.md) for the full release model, versioning strategy,
and release notes approach.
