"""
Integration loader.

Parses the ``integrations:`` list from ``config.yaml`` and builds an
``IntegrationRegistry`` populated with the appropriate source objects,
optional control adapters, and optional strategies.

Supported source types
----------------------
``generic_iobroker``
    Reads OID(s) directly from ioBroker simple-api.
    Required: ``power`` OR both ``power_import`` + ``power_export``.

``generic_homeassistant``
    Reads entity state(s) from the Home Assistant REST API.
    Required: ``power`` OR both ``power_import`` + ``power_export``.
    Needs ``ha_client`` to be provided; skipped gracefully when absent.

``template``
    Evaluates a Jinja2 template whose ``states("name.field")`` calls
    reference other integrations.  Required: ``power`` (template string).
    Registered last, evaluated after all data sources.

Supported control types
-----------------------
``homeassistant_switch``
    Calls ``homeassistant.turn_on/off`` via the HA REST API.
    Required: ``entity`` (HA entity ID).  Needs ``ha_client``.

``iobroker_switch``
    Writes ``True``/``False`` (or custom on/off values) to an ioBroker OID.
    Required: ``oid``.  Optional: ``on_value``, ``off_value``.

Supported strategy types
------------------------
``overflow``
    Turns the device on when PV surplus ≥ ``threshold_w``,
    off when surplus < ``threshold_w − hysteresis_w``.
    Optional keys: ``threshold_w`` (default 200), ``hysteresis_w`` (default 50).

Note: the optional ``tariff:`` key is accepted but reserved for future use.
"""

from __future__ import annotations

import logging
from typing import Any

from .integration_entry import IntegrationEntry
from .integration_registry import IntegrationRegistry
from ..plugins.generic_iobroker.source import GenericIoBrokerSource
from ..plugins.generic_homeassistant.source import GenericHASource
from ..plugins.template_source.source import TemplateSource
from ..plugins.homeassistant_switch.adapter import HASwitchAdapter
from ..plugins.iobroker_switch.adapter import IoBrokerSwitchAdapter
from ..plugins.overflow_strategy.strategy import OverflowStrategy

log = logging.getLogger(__name__)


def load_integrations(
    cfg_list: list[dict[str, Any]],
    iobroker_client: Any,
    ha_client: Any | None,
) -> IntegrationRegistry:
    """
    Build an ``IntegrationRegistry`` from the ``integrations:`` config list.

    Parameters
    ----------
    cfg_list:
        The value of ``config["integrations"]`` — a list of single-key dicts.
    iobroker_client:
        Open ioBroker client for ``generic_iobroker`` sources and
        ``iobroker_switch`` control adapters.
    ha_client:
        Open HA client for ``generic_homeassistant`` sources and
        ``homeassistant_switch`` control adapters, or ``None`` if Home
        Assistant is not configured.
    """
    registry = IntegrationRegistry()
    for entry in cfg_list or []:
        if not isinstance(entry, dict):
            continue
        for name, cfg in entry.items():
            _load_one(name, cfg or {}, registry, iobroker_client, ha_client)
    return registry


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _load_one(
    name: str,
    cfg: dict[str, Any],
    registry: IntegrationRegistry,
    iobroker_client: Any,
    ha_client: Any | None,
) -> None:
    role: str | None = cfg.get("role")

    # Accept either 'source' or 'consumer' as the section key.
    section: dict[str, Any] | None = cfg.get("source") or cfg.get("consumer")
    if section is None:
        log.warning("Integration %r has no 'source' or 'consumer' key — skipping", name)
        return

    src_type: str = section.get("type", "")

    source, is_template = _build_source(
        name, src_type, section, registry, iobroker_client, ha_client
    )
    if source is None:
        return

    # Optional control adapter
    control: Any = None
    control_cfg: dict[str, Any] | None = cfg.get("control")
    if control_cfg:
        control = _build_control(name, control_cfg, iobroker_client, ha_client)

    # Optional strategy (requires a control adapter)
    strategy: Any = None
    strategy_cfg: dict[str, Any] | None = cfg.get("strategy")
    if strategy_cfg:
        if control is None:
            log.warning(
                "Integration %r has a 'strategy' but no 'control' — strategy skipped",
                name,
            )
        else:
            strategy = _build_strategy(name, strategy_cfg, control)

    entry = IntegrationEntry(
        name=name,
        role=role,
        source=source,
        is_template=is_template,
        strategy=strategy,
    )
    registry.register_entry(entry)


def _build_source(
    name: str,
    src_type: str,
    section: dict[str, Any],
    registry: IntegrationRegistry,
    iobroker_client: Any,
    ha_client: Any | None,
) -> tuple[Any, bool]:
    """Return ``(source_object, is_template)`` or ``(None, False)`` on error."""

    if src_type == "generic_iobroker":
        has_power = "power" in section
        has_import_export = "power_import" in section and "power_export" in section
        if not has_power and not has_import_export:
            log.warning(
                "Integration %r (generic_iobroker): provide 'power' or both "
                "'power_import' and 'power_export' — skipping",
                name,
            )
            return None, False
        return (
            GenericIoBrokerSource(
                name=name,
                client=iobroker_client,
                oid_power=section.get("power"),
                oid_power_import=section.get("power_import"),
                oid_power_export=section.get("power_export"),
            ),
            False,
        )

    if src_type == "generic_homeassistant":
        if ha_client is None:
            log.warning(
                "Integration %r requires a Home Assistant client "
                "(homeassistant.url not configured) — skipping",
                name,
            )
            return None, False
        has_power = "power" in section
        has_import_export = "power_import" in section and "power_export" in section
        if not has_power and not has_import_export:
            log.warning(
                "Integration %r (generic_homeassistant): provide 'power' or both "
                "'power_import' and 'power_export' — skipping",
                name,
            )
            return None, False
        return (
            GenericHASource(
                name=name,
                client=ha_client,
                entity_power=section.get("power"),
                entity_power_import=section.get("power_import"),
                entity_power_export=section.get("power_export"),
            ),
            False,
        )

    if src_type == "template":
        if "power" not in section:
            log.warning("Integration %r (template) missing 'power' template — skipping", name)
            return None, False
        return (
            TemplateSource(
                name=name,
                registry=registry,
                power_template=section["power"],
            ),
            True,
        )

    log.warning("Integration %r has unknown source type %r — skipping", name, src_type)
    return None, False


def _build_control(
    name: str,
    cfg: dict[str, Any],
    iobroker_client: Any,
    ha_client: Any | None,
) -> Any:
    """Instantiate a control adapter from config.  Returns ``None`` on error."""
    ctrl_type: str = cfg.get("type", "")

    if ctrl_type == "homeassistant_switch":
        entity = cfg.get("entity")
        if not entity:
            log.warning(
                "Integration %r: homeassistant_switch missing 'entity' — skipping control",
                name,
            )
            return None
        if ha_client is None:
            log.warning(
                "Integration %r: homeassistant_switch needs a HA client — skipping control",
                name,
            )
            return None
        return HASwitchAdapter(ha_client, entity)

    if ctrl_type == "iobroker_switch":
        oid = cfg.get("oid")
        if not oid:
            log.warning(
                "Integration %r: iobroker_switch missing 'oid' — skipping control", name
            )
            return None
        return IoBrokerSwitchAdapter(
            iobroker_client,
            oid,
            on_value=cfg.get("on_value", True),
            off_value=cfg.get("off_value", False),
        )

    log.warning("Integration %r has unknown control type %r — skipping control", name, ctrl_type)
    return None


def _build_strategy(name: str, cfg: dict[str, Any], control: Any) -> Any:
    """Instantiate a strategy from config.  Returns ``None`` on error."""
    strat_type: str = cfg.get("type", "")

    if strat_type == "overflow":
        return OverflowStrategy(
            device=control,
            threshold_w=float(cfg.get("threshold_w", 200.0)),
            hysteresis_w=float(cfg.get("hysteresis_w", 50.0)),
        )

    log.warning(
        "Integration %r has unknown strategy type %r — skipping strategy",
        name, strat_type,
    )
    return None

