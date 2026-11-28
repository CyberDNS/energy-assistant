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
from .power_charge import POWER_CHARGES_SPEC


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
                "homeassistant": ParamSpec(
                    dict, label="Home Assistant",
                    description="Needed for every device read through Home Assistant, for "
                                "learning and for the Energy dashboard import. The add-on is "
                                "connected already",
                    fields={
                        "url": ParamSpec(str, required=True, label="URL",
                                         description="Address of Home Assistant, e.g. "
                                                     "http://homeassistant.local:8123"),
                        "token": ParamSpec(str, required=True, secret=True, label="Access token",
                                           description="Long-lived access token, created at the "
                                                       "bottom of your Home Assistant profile "
                                                       "under Security"),
                        "timeout_s": ParamSpec(float, default=10.0, advanced=True, **_POSITIVE,
                                               description="Seconds to wait for an answer from "
                                                           "Home Assistant"),
                    }),
                "mqtt": ParamSpec(
                    dict, label="MQTT broker",
                    description="Needed for Nornwatt's own entities in Home Assistant, openWB "
                                "over MQTT and Zendure with the local connection",
                    fields={
                        "host": ParamSpec(str, required=True,
                                          description="Host name or IP address of the broker"),
                        "port": ParamSpec(int, minimum=1, maximum=65535, default=1883,
                                          description="Port of the broker"),
                        "username": ParamSpec(str, description="Only if the broker asks for a "
                                                               "login"),
                        "password": ParamSpec(str, secret=True,
                                              description="Only if the broker asks for a login"),
                    }),
            },
        ),
        SectionSchema(
            "optimizer", "Optimizer", "How the plan is computed.",
            {
                "horizon_hours": ParamSpec(int, minimum=1, maximum=72, default=24,
                                           label="Planning horizon (h)",
                                           description="How far ahead the plan reaches"),
                "step_minutes": ParamSpec(int, minimum=5, maximum=60, default=60,
                                          label="Slot length (min)",
                                          description="Length of one step of the plan. Shorter "
                                                      "slots follow quarter-hourly prices but "
                                                      "take longer to compute"),
                "precision": ParamSpec(float, minimum=0, default=0.5, label="Precision (%)",
                                       description="How close to the best possible plan the "
                                                   "result must be. A smaller value takes "
                                                   "longer to compute"),
                "algorithm": ParamSpec(str, legacy=True, description="Only 'milp' exists; not used"),
            },
        ),
        SectionSchema(
            "controller", "Controller", "Polling, planning and control loop.",
            {
                "poll_interval_s": ParamSpec(float, label="Read devices every (s)", **_POSITIVE,
                                             description="Seconds between two reads of the "
                                                         "devices. Left empty, they are read "
                                                         "as often as they are controlled"),
                "control_interval_s": ParamSpec(float, default=30, label="Control every (s)",
                                                **_POSITIVE,
                                                description="Seconds between two updates of the "
                                                            "power sent to the devices"),
                "plan_interval_s": ParamSpec(float, default=900, label="Plan every (s)", **_POSITIVE,
                                             description="Seconds between two new plans"),
                "plan_lead_s": ParamSpec(float, minimum=0, default=30, advanced=True,
                                         label="Planning lead (s)",
                                         description="Planning starts this long before a slot "
                                                     "begins, so the plan is ready in time"),
                "dry_run": ParamSpec(bool, default=False,
                                     description="Nornwatt plans and logs what it would send, "
                                                 "but sends nothing to any device"),
                "plan_stale_after_min": ParamSpec(float, advanced=True, **_POSITIVE,
                                                  label="Release after no plan for (min)",
                                                  description="Without a current plan for this "
                                                              "long, every device goes back to "
                                                              "its own operation. Left empty: "
                                                              "45 min, or 3 planning intervals "
                                                              "if that is longer"),
                "control_stuck_after_min": ParamSpec(float, advanced=True, **_POSITIVE,
                                                     label="Release after no control for (min)",
                                                     description="When the control loop has not "
                                                                 "run for this long, every "
                                                                 "device goes back to its own "
                                                                 "operation. Left empty: 10 "
                                                                 "min, or 10 control intervals "
                                                                 "if that is longer"),
                "ev_feasibility_margin": ParamSpec(float, minimum=1, default=1.2, advanced=True,
                                                   label="EV deadline reserve factor",
                                                   description="A car's target counts as "
                                                               "reachable when the time left is "
                                                               "at least this many times the "
                                                               "charging time needed"),
            },
        ),
        SectionSchema(
            "server", "Server", "Web UI and API.",
            {
                "port": ParamSpec(int, minimum=1, maximum=65535, default=8088, restart=True,
                                  description="Port of the web UI and the API"),
                "api_token": ParamSpec(str, secret=True, restart=True, label="API token",
                                       description="With a token, requests from outside "
                                                   "Home Assistant must send it and can only "
                                                   "read. Left empty, the API is not protected"),
                "trusted_hosts": ParamSpec(list, items=ParamSpec(str), restart=True, advanced=True, item_label="address",
                                           description="Addresses that keep full access "
                                                       "without the token. Left empty: Home "
                                                       "Assistant (ingress) and this machine"),
                "history_retention_days": ParamSpec(float, default=8, label="Diagnostics kept (days)",
                                                    **_POSITIVE,
                                                    description="Days that past plans, the "
                                                                "record of what was sent and the "
                                                                "battery cost history are kept. "
                                                                "The device history is kept "
                                                                "separately"),
            },
        ),
        SectionSchema(
            "logging", "Logging", "How much Nornwatt writes to its log.",
            {"level": ParamSpec(str, default="INFO",
                                description="Messages of this level and above are logged. "
                                            "Debug is for finding problems",
                                choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                                choice_labels={"DEBUG": "Debug", "INFO": "Info",
                                               "WARNING": "Warning", "ERROR": "Error"})},
        ),
        SectionSchema(
            "energy_dashboard", "Energy dashboard import",
            "Take the consumers of the Home Assistant Energy dashboard over as devices.",
            {
                "import_consumers": ParamSpec(bool, default=False, label="Import consumers",
                                              description="Every individual device of the "
                                                          "Energy dashboard becomes a consumer "
                                                          "in Nornwatt"),
                "exclude": ParamSpec(list, items=ParamSpec(str), when={"import_consumers": True}, item_label="entry",
                                     description="Devices of the dashboard to leave out: the "
                                                 "energy or power sensor, the name shown in "
                                                 "Home Assistant, or the ID in Nornwatt"),
                "tariff": ParamSpec(str, tariff=True, when={"import_consumers": True},
                                    description="Tariff the imported consumers are billed on"),
                "learning": ParamSpec(object, widget="learning:consumer", when={"import_consumers": True},
                                      description="Whether and how the forecasts of the "
                                                  "imported consumers are learned"),
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
                                         description="The tariff at the grid connection, used "
                                                     "by every device that names no tariff"),
                    "power_charges": POWER_CHARGES_SPEC},
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
                              description="Sensor whose history shows the value. Left empty: "
                                          "the entity chosen under Temperature"),
    "switch_entity": ParamSpec(str, entity=True, label="Switch entity",
                               description="Entity whose history shows when the device ran. "
                                           "Left empty: the switch under Controlled by"),
    "modes": ParamSpec(dict, values=ParamSpec(str, entity=True), advanced=True,
                       description="For a device with two modes, instead of the switch "
                                   "entity: the name of each mode and the entity that is on "
                                   "in that mode"),
    "ambient_entity": ParamSpec(str, entity=True, label="Ambient temperature sensor",
                                description="Temperature around the device, e.g. of the room "
                                            "or outdoors. The drift depends on it"),
    "ambient_weather_entity": ParamSpec(str, entity=True, label="Weather forecast entity",
                                        description="Weather entity whose forecast gives the "
                                                    "ambient temperature of the coming hours"),
    "history_days": ParamSpec(float, default=10, **_POSITIVE,
                              description="Days of history the model is learned from"),
    "refit_interval_h": ParamSpec(float, default=6, **_POSITIVE,
                                  description="Hours between two refits, when the model is "
                                              "learned again from the history"),
}

#: Category → name of the ``learning:`` option set (``learning_options``).
LEARNING_KINDS: dict[DeviceCategory, str] = {
    DeviceCategory.CONSUMER: "consumer",
    DeviceCategory.THERMAL_LOAD: "thermal_load",
}
