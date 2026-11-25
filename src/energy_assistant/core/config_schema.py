"""Schemas of the config sections other than ``devices:``.

``devices:`` is described by ``core/device_model`` (category schemas) and the
device plugins.  This module describes the rest, with the same ``ParamSpec``
building blocks, so the loader can check the whole file and the configuration
UI can build a form for every part of it:

``SECTION_SCHEMAS``   sections that are one mapping of settings
                      (``backends``, ``optimizer``, ``controller``, ``server``,
                      ``logging``, ``energy_dashboard``).
``COLLECTIONS``       sections that hold entries keyed by id, each configured
                      through a plugin ``type`` (``tariffs``, ``forecasts``).
``LEARNING_SCHEMAS``  the options of a device's ``learning:`` per category.

Tariff and forecast plugins declare their own settings when they register
(``core/plugin_registry``); ``loader/config_validation`` applies all of it.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from .device_model import DeviceCategory, ParamSpec, specs_to_dict


@dataclass(frozen=True)
class SectionSchema:
    """One settings section of the config file."""

    name: str
    title: str
    description: str
    fields: Mapping[str, ParamSpec]

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "fields": specs_to_dict(self.fields),
        }


@dataclass(frozen=True)
class CollectionSchema:
    """A section of entries keyed by id, each built by a plugin ``type``."""

    name: str
    title: str
    description: str
    plugin_kind: str
    """``"tariff"`` or ``"forecast"``."""
    common: Mapping[str, ParamSpec]
    """Keys every entry accepts besides its plugin's settings."""
    scope: str = ""
    """Forecasts: only plugins with this scope."""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "plugin_kind": self.plugin_kind,
            "scope": self.scope,
            "common": specs_to_dict(self.common),
        }


_POSITIVE = {"minimum": 0, "exclusive_minimum": True}

SECTION_SCHEMAS: dict[str, SectionSchema] = {
    s.name: s
    for s in (
        SectionSchema(
            "backends", "Connections",
            "Home Assistant and the MQTT broker, shared by all devices that use them.",
            {
                "homeassistant": ParamSpec(dict, label="Home Assistant", fields={
                    "url": ParamSpec(str, required=True, label="URL",
                                     description="e.g. http://homeassistant.local:8123"),
                    "token": ParamSpec(str, required=True, secret=True, label="Access token",
                                       description="Long-lived access token"),
                    "timeout_s": ParamSpec(float, default=10.0, advanced=True, **_POSITIVE,
                                           description="Request timeout (s)"),
                }),
                "mqtt": ParamSpec(dict, label="MQTT broker", fields={
                    "host": ParamSpec(str, required=True, description="Host name or IP address"),
                    "port": ParamSpec(int, minimum=1, maximum=65535, default=1883),
                    "username": ParamSpec(str),
                    "password": ParamSpec(str, secret=True),
                }),
            },
        ),
        SectionSchema(
            "optimizer", "Optimizer", "How the plan is computed.",
            {
                "horizon_hours": ParamSpec(int, minimum=1, maximum=72, default=24,
                                           label="Planning horizon (h)",
                                           description="How far ahead the plan reaches (h)"),
                "step_minutes": ParamSpec(int, minimum=5, maximum=60, default=60,
                                          label="Slot length (min)",
                                          description="Length of a plan slot (min)"),
                "precision": ParamSpec(float, minimum=0, default=0.5, label="Precision (%)",
                                       description="Allowed gap to the optimum (%); smaller = "
                                                   "slower"),
                "algorithm": ParamSpec(str, legacy=True, description="Only 'milp' exists; not used"),
            },
        ),
        SectionSchema(
            "controller", "Controller", "Polling, planning and control loop.",
            {
                "poll_interval_s": ParamSpec(float, label="Read devices every (s)", **_POSITIVE,
                                             description="Seconds between device reads "
                                                         "(default: the control interval)"),
                "control_interval_s": ParamSpec(float, default=30, label="Control every (s)",
                                                **_POSITIVE,
                                                description="Seconds between setpoint updates"),
                "plan_interval_s": ParamSpec(float, default=900, label="Plan every (s)", **_POSITIVE,
                                             description="Seconds between planning runs"),
                "plan_lead_s": ParamSpec(float, minimum=0, default=30, advanced=True,
                                         label="Planning lead (s)",
                                         description="Start planning this long before a slot "
                                                     "begins (s)"),
                "dry_run": ParamSpec(bool, default=False,
                                     description="Log control commands instead of sending them"),
                "plan_stale_after_min": ParamSpec(float, advanced=True, **_POSITIVE,
                                                  label="Release after no plan for (min)",
                                                  description="Without a current plan for this "
                                                              "long every device goes back to its "
                                                              "own operation (default: 45 min, at "
                                                              "least 3 planning intervals)"),
                "control_stuck_after_min": ParamSpec(float, advanced=True, **_POSITIVE,
                                                     label="Release after no control for (min)",
                                                     description="Without a control tick for this "
                                                                 "long every device goes back to "
                                                                 "its own operation (default: 10 "
                                                                 "min, at least 10 control "
                                                                 "intervals)"),
                "ev_feasibility_margin": ParamSpec(float, minimum=1, default=1.2, advanced=True,
                                                   label="EV deadline reserve factor",
                                                   description="Time reserve factor before an EV "
                                                               "deadline counts as reachable"),
            },
        ),
        SectionSchema(
            "server", "Server", "Web UI and API.",
            {
                "port": ParamSpec(int, minimum=1, maximum=65535, default=8088, restart=True),
                "api_token": ParamSpec(str, secret=True, restart=True, label="API token",
                                       description="Bearer token for read-only access from "
                                                   "outside Home Assistant"),
                "trusted_hosts": ParamSpec(list, items=ParamSpec(str), restart=True, advanced=True, item_label="address",
                                           description="Addresses that need no token (default: "
                                                       "HA ingress and localhost)"),
                "history_retention_days": ParamSpec(float, default=8, label="Diagnostics kept (days)",
                                                    **_POSITIVE,
                                                    description="Days of battery-ledger history, plan "
                                                                "snapshots and execution records kept; "
                                                                "device history has its own two tiers"),
            },
        ),
        SectionSchema(
            "logging", "Logging", "",
            {"level": ParamSpec(str, default="INFO", description="Log level",
                                choices=("DEBUG", "INFO", "WARNING", "ERROR"))},
        ),
        SectionSchema(
            "energy_dashboard", "Energy dashboard import",
            "Take the consumers of the Home Assistant Energy dashboard over as devices.",
            {
                "import_consumers": ParamSpec(bool, default=False, label="Import consumers",
                                              description="Import the dashboard's individual "
                                                          "devices"),
                "exclude": ParamSpec(list, items=ParamSpec(str), when={"import_consumers": True}, item_label="entry",
                                     description="Entries to leave out: energy or power sensor, "
                                                 "name shown in HA, or device id"),
                "tariff": ParamSpec(str, tariff=True, when={"import_consumers": True}, 
                                    description="Tariff of the imported consumers"),
                "learning": ParamSpec(object, widget="learning:consumer", when={"import_consumers": True}, 
                                      description="Learned forecasts for the imported consumers"),
            },
        ),
    )
}

COLLECTIONS: dict[str, CollectionSchema] = {
    c.name: c
    for c in (
        CollectionSchema(
            "tariffs", "Tariffs", "Price models; devices and meters refer to them by id.",
            plugin_kind="tariff",
            common={"default": ParamSpec(bool, default=False, label="Default tariff",
                                         description="Used by everything that names no tariff")},
        ),
        CollectionSchema(
            "forecasts", "Forecasts", "System-wide forecasts such as PV generation.",
            plugin_kind="forecast", common={}, scope="system",
        ),
    )
}

#: Options of a thermal load's ``learning:`` (``plugins/learned/thermal``).
THERMAL_LEARNING = {
    "value_entity": ParamSpec(str, entity=True, label="Temperature sensor",
                              description="Temperature sensor (default: the bound temperature_c)"),
    "switch_entity": ParamSpec(str, entity=True, label="Switch entity",
                               description="Switch whose history shows when it ran (default: the "
                                           "controlling switch)"),
    "modes": ParamSpec(dict, values=ParamSpec(str, entity=True), advanced=True,
                       description="Instead of switch_entity for two-mode devices: mode → "
                                   "entity that is on in that mode"),
    "ambient_entity": ParamSpec(str, entity=True, label="Ambient temperature sensor", description="Ambient temperature sensor"),
    "ambient_weather_entity": ParamSpec(str, entity=True, label="Weather forecast entity",
                                        description="Weather entity for the ambient forecast"),
    "history_days": ParamSpec(float, default=10, **_POSITIVE, description="Days of history used"),
    "refit_interval_h": ParamSpec(float, default=6, **_POSITIVE, description="Hours between refits"),
}

#: Category → name of the ``learning:`` option set (``learning_options``).
LEARNING_KINDS: dict[DeviceCategory, str] = {
    DeviceCategory.CONSUMER: "consumer",
    DeviceCategory.THERMAL_LOAD: "thermal_load",
}
