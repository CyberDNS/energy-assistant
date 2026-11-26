<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="branding/nornwatt-logo-on-dark.svg">
    <img alt="Nornwatt" src="branding/nornwatt-logo-on-light.svg" height="64">
  </picture>
</p>

<p align="center">
  Open-source, vendor-neutral energy management for your home.<br>
  Plans your battery, car and heating around solar, prices and your household, and controls them.
</p>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/home-dark.png">
  <img alt="The Nornwatt home page: live power flow, today's figures and the day's plan" src="docs/images/home-light.png">
</picture>

## What it does

- **Plans ahead.** Every 15 minutes an optimizer plans the coming hours and days from
  price, solar and consumption forecasts: when to charge or discharge the battery,
  charge the car, run the heater or the cooler.
- **Controls your devices, or only watches.** Each device starts in observe mode and is
  handed over when you are ready. If Nornwatt stops, every device goes back to its own logic.
- **Works with what you have.** Anything Home Assistant can read or command, plus
  direct support for SMA (Modbus), Zendure, openWB, Tasmota, Tibber and ENTSO-E prices.
- **Learns your house:** consumption from Home Assistant's history, thermal models of
  heating and cooling, the real efficiency of each battery.
- **Shows what it saves** against a simulated house without planning, in the app and
  as Home Assistant sensors.

## Getting started

The easiest way to run Nornwatt is as a Home Assistant add-on:

1. Add `https://github.com/CyberDNS/hassio-addons-repository` under
   **Settings → Add-ons → Add-on store → ⋮ → Repositories**.
2. Install **Nornwatt** and start it. No configuration file is needed.
3. Open **Nornwatt** in the sidebar and add your grid meter, then your tariff.

Container images, file locations and the first start in detail:
[Installation](docs/installation.md).

> Nornwatt used to be called **Energy Assistant**. To move an existing installation over, see
> [Moving from Energy Assistant](docs/installation.md#moving-from-energy-assistant).

## Documentation

- [Installation](docs/installation.md)
- [The web UI](docs/web-ui.md)
- [Configuration](docs/configuration.md) and [Devices](docs/devices.md)
- [Control and safety](docs/control.md)
- [Learning](docs/learning.md)
- [Nornwatt in Home Assistant](docs/home-assistant.md)
- [API and MCP](docs/api.md)
- [Development](docs/development.md)

Background: [VISION.md](VISION.md) · [ARCHITECTURE.md](ARCHITECTURE.md) · [ROADMAP.md](ROADMAP.md)

## Support

- **Something doesn't work?** Open a [problem report](https://github.com/CyberDNS/nornwatt/issues/new?template=bug_report.yml).
- **Your device isn't supported yet?** Open a [device request](https://github.com/CyberDNS/nornwatt/issues/new?template=device_request.yml).
- **Questions and ideas:** [GitHub Discussions](https://github.com/CyberDNS/nornwatt/discussions).

Please don't post passwords, tokens or IP addresses in issues: they are public.
