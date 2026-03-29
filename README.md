# Energy Assistant

An open-source, vendor-neutral energy management platform for homeowners.

See [VISION.md](VISION.md) for the project vision and [ARCHITECTURE.md](ARCHITECTURE.md) for the technical design.

## Container images

Pre-built multi-arch images are published to GHCR:

```
ghcr.io/cyberdns/energy-assistant:dev      # latest main branch (dev channel)
ghcr.io/cyberdns/energy-assistant:edge     # latest prerelease tag (edge channel)
ghcr.io/cyberdns/energy-assistant:latest   # latest stable tag (prod channel)
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

- Config file (user-editable): `/config/energy-assistant/config.yaml`
- SQLite DB (private, persistent): `/data/energy-assistant.db`

If you want users to inspect/back up the DB via File Editor, set
`ENERGY_ASSISTANT_DB=/config/energy-assistant/energy-assistant.db`
and map `addon_config` with write access in the add-on definition.

The app supports overriding both paths via environment variables:

- `ENERGY_ASSISTANT_CONFIG`
- `ENERGY_ASSISTANT_DB`

Two runtime use cases are supported by default:

| Use case | Runtime hint | Default config path | Default DB path |
|---|---|---|---|
| Local development (VS Code) | `ENERGY_ASSISTANT_MODE=local` (optional) | `./config.yaml` | `./data/history.db` |
| Home Assistant add-on | `ENERGY_ASSISTANT_MODE=ha` (optional) or auto-detected via `/data/options.json` | `/config/energy-assistant/config.yaml` | `/data/energy-assistant.db` |
