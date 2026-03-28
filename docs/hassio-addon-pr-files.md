# Home Assistant Add-on Repository PR — Energy Assistant Dev

This document contains all files to be added to
[CyberDNS/hassio-addons-repository](https://github.com/CyberDNS/hassio-addons-repository)
in order to publish the **Energy Assistant (dev)** Home Assistant add-on.

---

## How to apply these changes

```bash
# 1. Fork & clone the repository
git clone https://github.com/CyberDNS/hassio-addons-repository.git
cd hassio-addons-repository

# 2. Create a feature branch
git checkout -b feat/add-energy-assistant-dev

# 3. Create the add-on directory
mkdir energy-assistant-dev

# 4. Copy each file below into the correct path (see sections below)

# 5. Commit and push
git add energy-assistant-dev/ Readme.MD
git commit -m "feat: add Energy Assistant dev add-on"
git push origin feat/add-energy-assistant-dev

# 6. Open a pull request against CyberDNS/hassio-addons-repository main/master
```

---

## File: `energy-assistant-dev/config.json`

```json
{
  "name": "Energy Assistant (dev)",
  "version": "0.1.0-dev",
  "slug": "energy_assistant_dev",
  "description": "Open-source, vendor-neutral energy management platform (development channel — unstable)",
  "url": "https://github.com/CyberDNS/energy-assistant",
  "startup": "application",
  "boot": "auto",
  "init": false,
  "arch": [
    "aarch64",
    "amd64",
    "armv7"
  ],
  "image": "ghcr.io/cyberdns/energy-assistant:dev",
  "ports": {
    "8088/tcp": 8088
  },
  "ingress": true,
  "ingress_port": 8088,
  "map": [
    "config:rw",
    "data:rw"
  ],
  "options": {
    "log_level": "info"
  },
  "schema": {
    "log_level": "str"
  }
}
```

---

## File: `energy-assistant-dev/Dockerfile`

```dockerfile
ARG BUILD_FROM
FROM ghcr.io/cyberdns/energy-assistant:dev
```

> **Note:** Because `image` is set in `config.json`, Home Assistant pulls directly
> from GHCR and does not build this Dockerfile during normal add-on installation.
> It is included for convention compatibility with the existing add-ons in this
> repository and for users who wish to build locally.

---

## File: `energy-assistant-dev/README.md`

```markdown
# Home Assistant Add-on: Energy Assistant (dev)

![Warning](https://img.shields.io/badge/stability-unstable-red)

> ⚠️ **This is the development / unstable channel.**
> It consumes nightly images built from the `main` branch of the
> [energy-assistant](https://github.com/CyberDNS/energy-assistant) project.
> Do **not** use this add-on in a production environment.

## What is Energy Assistant?

Energy Assistant is an open-source, vendor-neutral energy management platform
for Home Assistant. It helps you monitor, analyse, and optimise your household
energy consumption and generation.

## Installation

1. Add this repository to your Home Assistant add-on store:
   `https://github.com/CyberDNS/hassio-addons-repository`
2. Find **Energy Assistant (dev)** in the add-on store and click **Install**.
3. Configure the add-on (see Configuration below).
4. Start the add-on.

## Configuration

| Option      | Default | Description                              |
| ----------- | ------- | ---------------------------------------- |
| `log_level` | `info`  | Log verbosity: `debug`, `info`, `warning`, `error` |

The add-on mounts your Home Assistant `config` and `data` folders as
read-write volumes, so configuration and persistent data survive restarts.

## Ports

The application listens on port **8088** and is also accessible via the
Home Assistant ingress panel.

## Issues & Support

Please open issues on the main project page, **not** on this repository:
➡️ [https://github.com/CyberDNS/energy-assistant/issues](https://github.com/CyberDNS/energy-assistant/issues)
```

---

## File: `energy-assistant-dev/CHANGELOG.md`

```markdown
# Changelog

## 0.1.0-dev

- Initial dev channel release
- Consumes nightly images from ghcr.io/cyberdns/energy-assistant:dev
- For development and testing purposes only
```

---

## Updated: `Readme.MD` (repository root)

Replace the current content with the following (adds Energy Assistant to the
add-on table):

```markdown
# Cyberdns' Home Assistant Addon Repository

This repository can be configured under HA addon settings page.

The main addon that is provided by me is the Lupusec2Mqtt addon.

⚠️ Please open all issues on their relevant Github project pages. Issues on this repository will be ignored! ⚠️

| Addon                     | Github project page                                                                                      |
| ------------------------- | -------------------------------------------------------------------------------------------------------- |
| Lupusec2Mqtt              | [https://github.com/CyberDNS/Lupusec2Mqtt](https://github.com/CyberDNS/Lupusec2Mqtt)                   |
| Energy Assistant (dev)    | [https://github.com/CyberDNS/energy-assistant](https://github.com/CyberDNS/energy-assistant)           |
```

---

## PR description (suggested)

> **feat: add Energy Assistant dev add-on**
>
> Adds the `energy-assistant-dev` add-on directory for the Energy Assistant
> development channel. The add-on pulls the pre-built dev image from
> `ghcr.io/cyberdns/energy-assistant:dev` (published by the energy-assistant
> CI pipeline on every push to `main`).
>
> Changes:
> - `energy-assistant-dev/config.json` — add-on metadata and schema
> - `energy-assistant-dev/Dockerfile` — minimal passthrough for local builds
> - `energy-assistant-dev/README.md` — installation and configuration docs
> - `energy-assistant-dev/CHANGELOG.md` — initial changelog entry
> - `Readme.MD` — added Energy Assistant to the repository add-on table
