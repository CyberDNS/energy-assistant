# The web UI

The UI has a sidebar, a status bar at the bottom that always shows what the
house is doing (solar, grid, battery, cars, prices), and a notice at the top of
every page when something needs attention: `config.yaml` changed on disk, dry
run, devices that do not respond.

| Group | Pages |
|---|---|
| Overview | **Home** (power flow, today on one timeline, what the plan does next, today's figures, cars), **Plan** (the optimizer's plan) |
| Devices | **Cars**, **Batteries**, **Heating & loads** (each only when the house has such devices) and **All devices** |
| Insights | **Savings** and **Learning** for the whole house, with links to the devices |
| System | **Settings** (see [Configuring in the UI](configuration.md#configuring-in-the-ui)) |

Every device has its own page with the same sections in the same order: now,
plan, history, what it saved, what was learned about it. Sections a device has
nothing for are left out.

**Charts** react to time only: drag to move in time, pinch (or Ctrl + scroll)
to zoom in time, double-click or *Show all* to see everything again. The
vertical axis stays fixed.

**History** is kept by Nornwatt itself in two compact tiers: 5-minute
averages for about two weeks and hourly averages for good (24 rows per device
and day). `GET /api/history` picks the tier by the range asked for. A database
from before this (every reading stored, 8 days) is converted in the background
at the first start.

**In Home Assistant** (2026.2 or later) the add-on takes over the whole
screen: Home Assistant hides its own sidebar and header, and the **Home
Assistant** button at the bottom of the menu brings Home Assistant's sidebar
back. Home Assistant's URL follows the page that is open, so links and reloads
come back to it. To keep both sidebars side by side, turn off *Use the whole
screen in Home Assistant* under Settings → User interface; that setting and the
theme (light, dark, like the device) are stored per browser.

## Embedding the UI in a dashboard

Every page can be deep-linked and embedded (e.g. in an iframe / webpage card):

| URL | Shows |
|---|---|
| `#/plan` | Plan |
| `#/savings` | Savings against an uncontrolled house over rolling windows (24 h – 1 y) |
| `#/cars/<vehicle>` | One car: target, 7-day strip, weekly plan, "Charge now" and its charging-plan chart. `<vehicle>` is the vehicle id or label (case-insensitive). |
| `#/devices/<device>` | One device (id as in `config.yaml`) |
| `#/batteries`, `#/loads`, `#/devices`, `#/learning` | The page of that name |
| `#/settings/<page>` | A settings page: `devices`, `tariffs`, `forecasts`, `connections`, `system`, `secrets`, `interface` |
| `…?embed=1` | Content only: no sidebar, header or status bar. Combine with any route, e.g. `#/cars/bandit?embed=1`. |

Hosts that drop the URL fragment (the Home Assistant ingress panel does) can
pass the same options as a query string on the page URL instead:
`?view=plan`, `?view=savings`, `?vehicle=<vehicle>`, `?embed=1` — e.g. `/?vehicle=bandit&embed=1`.
When opened without any route, the UI restores the page used last.

Older links keep working: `#/plan/<vehicle>` and `#/plan?vehicle=<vehicle>`
open the car, `#/config/<page>` the settings page, `#/history` the device list.

The UI makes no external requests: all libraries are part of the build.
