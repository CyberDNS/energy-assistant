"""Configuration editor API: config.yaml and secrets.yaml, edited from the UI.

Endpoints (all under ``/api/config``)::

    GET    /schema                  every schema the UI builds its forms from
    GET    /file                    the content of config.yaml, in structured form
    GET    /entities                Home Assistant entity ids (for pickers)
    POST   /validate                check one change without saving
    PUT    /devices/{id}/control    observe / active, applied at once (no reload)
    PUT    /devices/{id}            create or change a device        ┐
    PUT    /tariffs/{id}            … a tariff                       │ then apply
    PUT    /forecasts/{id}          … a system-wide forecast         │ without a
    DELETE /{collection}/{id}       remove a device/tariff/forecast  │ restart
    PUT    /sections/{name}         replace a settings section       ┘
    PUT    /secrets/{name}          set a secret in secrets.yaml (the value is write-only)
    DELETE /secrets/{name}          remove a secret that nothing uses
    POST   /reload                  apply config.yaml as it is on disk
    GET    /validate                the problems of config.yaml as it is on disk
    GET    /text                    config.yaml as text (the editor of last resort)
    PUT    /text                    replace config.yaml with a text and apply it

A change is first applied to the file text in memory (``config/store``) and
that text is checked exactly as a start with it would be
(``Application.check_config_text``).  Only a valid file is written; the
running configuration is then rebuilt without a restart
(``Application.reload_config``).  If the rebuild fails, the previous file is
restored.

While config.yaml has problems ("configuration needed": nothing is planned or
controlled, see ``Application.config_problems``) a change is also saved when
it is not valid yet, as long as it adds no new problem — so the valid parts
can be edited and the problems fixed one by one.  The fixed file is applied
at once.  A running configuration is never replaced by one with problems.

Secrets never leave the server: a ``!secret name`` value is exchanged as
``{"!secret": name}``, a plain value of a setting declared ``secret`` as
``{"!hidden": true}`` (which on the way back means "keep what is in the file").
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from ..config.store import (
    ConfigConflictError,
    ConfigDocument,
    ConfigStoreError,
    DeviceConfigStore,
    SecretsStore,
    entry_to_form,
    form_to_entry,
    mask_secrets,
    prune_empty,
    revision_of,
    unmask_secrets,
)
from ..core.config_schema import COLLECTIONS, LEARNING_KINDS, SECTION_SCHEMAS
from ..core.device_model import (
    CAPABILITY_TITLES,
    CATEGORY_SCHEMAS,
    CONTROL_MODES,
    CONTROLLED_CATEGORIES,
    MODEL_KEYS,
    OBSERVE,
    Capability,
    DeviceCategory,
    DeviceConfigError,
    ParamSpec,
    history_fields,
    specs_to_dict,
)
from ..core.history_import import FIELDS as HISTORY_FIELD_LABELS
from ..core.presets import presets_json
from ..loader.config_validation import learning_options
from ..plugins import registry as plugin_registry

if TYPE_CHECKING:
    from . import Application

_log = logging.getLogger(__name__)

_ENV_PREFIX = "NORNWATT_SECRET_"

_CONTROLLED = {c.value for c in CONTROLLED_CATEGORIES}


class ChangeBody(BaseModel):
    """One change to the config file."""

    kind: str | None = None
    """For ``/validate``: ``devices``, ``tariffs``, ``forecasts`` or ``section``."""
    id: str | None = None
    """Entry id or section name (taken from the URL on PUT)."""
    value: dict[str, Any]
    """Device form, ``{type, settings}`` of a tariff / forecast, or the
    section's values."""
    revision: str | None = None
    """Revision of the file the edit is based on (from ``GET /file``)."""
    create: bool = False
    """True when adding an entry: fails if the id already exists."""


class ControlBody(BaseModel):
    control: str
    """``observe`` or ``active``."""


class SecretBody(BaseModel):
    value: str


class TextBody(BaseModel):
    text: str
    revision: str | None = None



def _history_specs(category: DeviceCategory, field_name: str) -> dict[str, ParamSpec]:
    """The keys of one ``history:`` field: those of a binding of the same
    value — one entity, or for power one per direction with the same switch
    as bindings — without what the import works out itself (the sign) or
    does not take (attribute, scale)."""
    from ..plugins._homeassistant.bindings import (  # noqa: PLC0415
        CHARGE_DISCHARGE,
        IMPORT_EXPORT,
        binding_specs,
    )
    if field_name == "soc":
        specs = {k: v for k, v in binding_specs(Capability.SOC_PCT).items() if k == "entity"}
    else:
        directions = (CHARGE_DISCHARGE if category in (DeviceCategory.BATTERY, DeviceCategory.VEHICLE)
                      else IMPORT_EXPORT)
        specs = {k: v for k, v in binding_specs(Capability.POWER_W, directions=directions).items()
                 if k not in ("invert_sign", "attribute", "scale")}
    # The whole section is optional; a split pair is checked when the file is
    # validated (core/device_model.history_source_problem).
    return {k: dataclasses.replace(v, required=False) for k, v in specs.items()}

def _errors_of(exc: Exception) -> list[str]:
    if isinstance(exc, DeviceConfigError):
        return list(exc.errors)
    return [str(exc) or type(exc).__name__]


def _device_setting_specs(type_name: Any) -> dict[str, ParamSpec] | None:
    info = plugin_registry.device_info(type_name) if isinstance(type_name, str) else None
    return dict(info.settings) if info is not None and info.settings is not None else None


def _entry_specs(collection: str, type_name: Any) -> dict[str, ParamSpec] | None:
    """Setting specs of a tariff / forecast entry (plugin settings + common keys)."""
    schema = COLLECTIONS[collection]
    info = None
    if isinstance(type_name, str):
        info = (plugin_registry.tariff_info(type_name) if schema.plugin_kind == "tariff"
                else plugin_registry.forecast_info(type_name))
    if info is None or info.settings is None:
        return None
    return {**info.settings, **schema.common}


def mount_config_api(api: FastAPI, app: Application) -> None:
    edit_lock = asyncio.Lock()

    def _store() -> DeviceConfigStore:
        return DeviceConfigStore(app.config_path)

    def _secrets() -> SecretsStore:
        return SecretsStore(app.config_path.parent / "secrets.yaml")

    def _read() -> ConfigDocument:
        try:
            return _store().read()
        except ConfigStoreError as exc:
            raise HTTPException(409, {"errors": [str(exc)]}) from exc

    def _edited_text(doc: ConfigDocument, kind: str, item_id: str, value: dict[str, Any],
                     create: bool) -> str:
        """The file text with one change applied (nothing is written)."""
        if kind == "section":
            if item_id not in SECTION_SCHEMAS:
                raise ConfigStoreError(f"unknown section {item_id!r}")
            values = prune_empty(unmask_secrets(value, doc.section(item_id)))
            return doc.with_section(item_id, values)
        if kind not in ("devices", *COLLECTIONS):
            raise ConfigStoreError(f"unknown kind {kind!r}")
        if not item_id:
            raise ConfigStoreError("an id is needed")
        exists = item_id in doc.ids(kind)
        if create and exists:
            raise ConfigStoreError(f"{kind}: id {item_id!r} already exists")
        if not create and not exists:
            raise ConfigStoreError(f"{kind}: {item_id!r} is not in config.yaml")
        existing = doc.entry(kind, item_id)
        if kind == "devices":
            if value.get("id") not in (None, item_id):
                raise ConfigStoreError("a device id cannot be changed (stored data is keyed by it)")
            if create and value.get("control") is None and value.get("category") in _CONTROLLED:
                # New devices start in observe mode (issue #134).
                value = {**value, "control": OBSERVE}
            return doc.with_entry(kind, item_id, form_to_entry(value, existing))
        settings = value.get("settings") or {}
        if not isinstance(settings, Mapping) or "type" in settings:
            raise ConfigStoreError("'settings' must be a mapping without 'type'")
        entry = {"type": value.get("type"),
                 **prune_empty(unmask_secrets(settings, existing or {}))}
        return doc.with_entry(kind, item_id, prune_empty(entry))

    async def _problems(text: str) -> list[str]:
        """Every problem of *text* as config file content (empty: valid)."""
        try:
            await app.check_config_text(text)
        except Exception as exc:  # noqa: BLE001
            return _errors_of(exc)
        return []

    async def _apply(new_text: str, doc: ConfigDocument | None, revision: str | None,
                     *, allow_problems: bool = False) -> dict[str, Any]:
        """Check, write and reload; restores the file if the reload fails.

        While the app waits for a configuration, *new_text* is saved even
        with problems — if they were all there before (or *allow_problems*,
        for the text editor) — and the result lists them."""
        problems = await _problems(new_text)
        if problems:
            if not app.config_problems:
                raise HTTPException(422, {"errors": problems})
            if not allow_problems:
                before = await _problems(doc.text) if doc is not None else []
                new = [p for p in problems if p not in before]
                if new:
                    raise HTTPException(422, {"errors": new})
        previous = doc.text if doc is not None else _store().text()
        try:
            new_revision = _store().write(
                new_text, expected_revision=revision or revision_of(previous)
            )
        except ConfigConflictError as exc:
            raise HTTPException(409, {"errors": [str(exc)]}) from exc
        try:
            await app.reload_config()
        except Exception as exc:
            if app.config_problems:
                # Not running before either: keep the file, report what is left.
                return {"ok": True, "revision": new_revision, "applied": False,
                        "problems": app.config_problems}
            _log.error("Applying the edited config failed; restoring the previous file: %s", exc)
            _store().write(previous)
            raise HTTPException(
                500, {"errors": [
                    ("could not apply the change — the previous configuration is running again"),
                    *_errors_of(exc),
                ]}
            ) from exc
        return {"ok": True, "revision": new_revision, "applied": True, "problems": []}

    async def _change(kind: str, item_id: str, body: ChangeBody) -> dict[str, Any]:
        async with edit_lock:
            doc = _read()
            try:
                new_text = _edited_text(doc, kind, item_id, body.value, body.create)
            except ConfigStoreError as exc:
                raise HTTPException(422, {"errors": [str(exc)]}) from exc
            result = await _apply(new_text, doc, body.revision)
            _log.info("Config: %s %r %s from the UI", kind, item_id,
                      "added" if body.create else "changed")
            return result

    # ── reading ────────────────────────────────────────────────────────

    @api.get("/api/config/schema")
    async def get_config_schema() -> dict:
        """Everything the config UI needs to build its forms."""
        return {
            "categories": [schema.to_dict() for schema in CATEGORY_SCHEMAS.values()],
            "capabilities": [
                {"capability": c.value, "title": CAPABILITY_TITLES.get(c.value, c.value)}
                for c in Capability
            ],
            "plugins": [info.to_dict() for info in plugin_registry.device_infos()],
            "tariff_plugins": [info.to_dict() for info in plugin_registry.tariff_infos()],
            "forecast_plugins": [info.to_dict() for info in plugin_registry.forecast_infos()],
            "sections": [schema.to_dict() for schema in SECTION_SCHEMAS.values()],
            "collections": [schema.to_dict() for schema in COLLECTIONS.values()],
            "learning": {
                kind: specs_to_dict(learning_options(kind))
                for kind in sorted(set(LEARNING_KINDS.values()))
            },
            "learning_kinds": {cat.value: kind for cat, kind in LEARNING_KINDS.items()},
            # Past data from Home Assistant (core/history_import): per category
            # and field its label and the binding keys it takes.
            "history_fields": {
                cat.value: {f: {"label": HISTORY_FIELD_LABELS[f], "specs": specs_to_dict(_history_specs(cat, f))}
                            for f in history_fields(cat)}
                for cat in DeviceCategory
            },
            "model_keys": sorted(MODEL_KEYS),
            # Categories with control: observe | active (issue #134).
            "controlled_categories": sorted(_CONTROLLED),
            "presets": presets_json(),
        }

    @api.get("/api/config/file")
    async def get_config_file() -> dict:
        """config.yaml in structured form, without any secret value."""
        doc = _read()
        entries = doc.entries("devices")
        devices = [
            entry_to_form(device_id, entry, _device_setting_specs(entry.get("type")))
            for device_id, entry in entries.items()
        ]
        # Devices that are running but not in the file (HA Energy dashboard import).
        imported = [
            {**entry_to_form(spec.id, spec.to_dict()), "imported": True}
            for spec in app.running_devices().values()
            if spec.id not in entries and spec.energy_dashboard
        ]

        def collection(name: str) -> list[dict[str, Any]]:
            out = []
            for entry_id, entry in doc.entries(name).items():
                settings = {k: v for k, v in entry.items() if k != "type"}
                out.append({
                    "id": entry_id,
                    "type": entry.get("type"),
                    "settings": mask_secrets(settings, _entry_specs(name, entry.get("type"))),
                })
            return out

        refs = doc.secret_refs()
        try:
            names = _secrets().names()
        except ConfigStoreError as exc:
            raise HTTPException(409, {"errors": [str(exc)]}) from exc
        secrets = [
            {
                "name": name,
                "used_by": refs.get(name, []),
                # An environment variable of that name wins over secrets.yaml.
                "from_env": _ENV_PREFIX + name.upper().replace("-", "_") in os.environ,
            }
            for name in sorted({*names, *refs})
        ]
        for secret in secrets:
            secret["defined"] = secret["name"] in names or secret["from_env"]
        return {
            "revision": doc.revision,
            "configuration": app.configuration_state(),
            # The file differs from what is running (edited by hand, not yet applied).
            "pending_reload": doc.revision != revision_of(app.loaded_config_text),
            "devices": devices,
            "imported": imported,
            "tariffs": collection("tariffs"),
            "forecasts": collection("forecasts"),
            "sections": {
                name: mask_secrets(doc.section(name), schema.fields)
                for name, schema in SECTION_SCHEMAS.items()
            },
            "secrets": secrets,
        }

    @api.get("/api/config/state")
    async def get_config_state() -> dict:
        """Whether config.yaml on disk differs from what is running.

        Cheap enough to poll: the UI shows a notice on every page while a
        hand-edited file waits to be applied.
        """
        revision = _store().revision()
        return {
            "revision": revision,
            "pending_reload": revision != revision_of(app.loaded_config_text),
            "configuration": app.configuration_state(),
        }

    @api.get("/api/config/entities")
    async def get_config_entities() -> list[dict]:
        """Home Assistant entities for the entity selectors (id, name, state,
        unit, area, options, attributes — ``HAClient.entity_catalog``); empty
        without a Home Assistant backend or when it cannot be reached."""
        client = app.ha_client()
        if client is None:
            return []
        try:
            return await client.entity_catalog()
        except Exception as exc:  # noqa: BLE001
            _log.info("Could not list Home Assistant entities: %s", exc)
            return []

    # ── changing ───────────────────────────────────────────────────────

    @api.get("/api/config/validate")
    async def validate_config_file() -> dict:
        """Every problem of config.yaml as it is on disk."""
        text = _store().text()
        problems = await _problems(text)
        return {"ok": not problems, "errors": problems, "revision": revision_of(text)}

    @api.post("/api/config/validate")
    async def validate_config_change(body: ChangeBody) -> dict:
        """Check one change (as it would be saved) against the whole config.

        While config.yaml has problems, only the problems this change adds
        are ``errors`` (it can be saved without them); ``other_problems``
        are those of the file that remain."""
        try:
            doc = _store().read()
            text = _edited_text(doc, body.kind or "", body.id or "", body.value, body.create)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "errors": _errors_of(exc), "other_problems": []}
        problems = await _problems(text)
        if problems and app.config_problems:
            before = await _problems(doc.text)
            new = [p for p in problems if p not in before]
            return {"ok": not new, "errors": new,
                    "other_problems": [p for p in problems if p in before]}
        return {"ok": not problems, "errors": problems, "other_problems": []}

    @api.get("/api/config/text")
    async def get_config_text() -> dict:
        """config.yaml as text, with its problems."""
        text = _store().text()
        return {"text": text, "revision": revision_of(text), "errors": await _problems(text)}

    @api.put("/api/config/text")
    async def put_config_text(body: TextBody) -> dict:
        """Replace config.yaml and apply it.  While the app runs, only a valid
        file is accepted; while it waits for a configuration, any text is
        saved (and its problems listed)."""
        async with edit_lock:
            result = await _apply(body.text, None, body.revision, allow_problems=True)
            _log.info("Config: config.yaml replaced from the UI text editor")
            return result

    @api.put("/api/config/devices/{device_id}")
    async def put_config_device(device_id: str, body: ChangeBody) -> dict:
        """Create or change one device in config.yaml and apply it."""
        return await _change("devices", device_id, body)

    @api.put("/api/config/devices/{device_id}/control")
    async def put_device_control(device_id: str, body: ControlBody) -> dict:
        """Switch a device between observe mode (planned, nothing sent) and
        active control: writes ``control:`` into its entry in config.yaml and
        applies it without a reload — active from the next control tick,
        observe releases the device at once (issue #134)."""
        if body.control not in CONTROL_MODES:
            raise HTTPException(422, {"errors": [f"control must be one of {', '.join(CONTROL_MODES)}"]})
        async with edit_lock:
            doc = _read()
            entry = doc.entry("devices", device_id)
            running = app.running_devices().get(device_id)
            if entry is None or running is None:
                raise HTTPException(404, {"errors": [f"device {device_id!r} is not in config.yaml"]})
            if not running.controlled:
                raise HTTPException(422, {"errors": [
                    f"{device_id!r} is not a device Nornwatt controls"]})
            new_text = doc.with_entry("devices", device_id, {**entry, "control": body.control})
            if app.config_problems or doc.revision != revision_of(app.loaded_config_text):
                # Not running as in the file: save and apply it as a whole.
                result = await _apply(new_text, doc, doc.revision)
            else:
                problems = await _problems(new_text)
                if problems:
                    raise HTTPException(422, {"errors": problems})
                try:
                    revision = _store().write(new_text, expected_revision=doc.revision)
                except ConfigConflictError as exc:
                    raise HTTPException(409, {"errors": [str(exc)]}) from exc
                await app.set_device_control(device_id, body.control, new_text)
                result = {"ok": True, "revision": revision, "applied": True, "problems": []}
            _log.info("Config: device %r set to control: %s from the UI", device_id, body.control)
            return {**result, "control": body.control}

    @api.put("/api/config/tariffs/{tariff_id}")
    async def put_config_tariff(tariff_id: str, body: ChangeBody) -> dict:
        """Create or change one tariff and apply it."""
        return await _change("tariffs", tariff_id, body)

    @api.put("/api/config/forecasts/{forecast_id}")
    async def put_config_forecast(forecast_id: str, body: ChangeBody) -> dict:
        """Create or change one system-wide forecast and apply it."""
        return await _change("forecasts", forecast_id, body)

    @api.put("/api/config/sections/{name}")
    async def put_config_section(name: str, body: ChangeBody) -> dict:
        """Replace one settings section (backends, optimizer, …) and apply it."""
        schema = SECTION_SCHEMAS.get(name)
        if schema is None:
            raise HTTPException(404, {"errors": [f"unknown section {name!r}"]})
        before = _read().section(name)
        result = await _change("section", name, body)
        after = _read().section(name)
        # Settings that only a restart applies (server port, API token).
        result["restart_required"] = [
            key for key, spec in schema.fields.items()
            if spec.restart and before.get(key) != after.get(key)
        ]
        return result

    # ── secrets ────────────────────────────────────────────────────────

    @api.put("/api/config/secrets/{name}")
    async def put_config_secret(name: str, body: SecretBody) -> dict:
        """Set a secret in secrets.yaml.  If the configuration uses it, the
        configuration is applied again with the new value."""
        async with edit_lock:
            doc = _read()
            store = _secrets()
            path = store.path
            previous = path.read_text() if path.exists() else None
            try:
                store.set(name, body.value)
            except ConfigStoreError as exc:
                raise HTTPException(422, {"errors": [str(exc)]}) from exc
            if name in doc.secret_refs():
                before = app.config_problems
                try:
                    await app.reload_config()
                except Exception as exc:
                    # Waiting for a configuration and no new problem: keep it.
                    new = [p for p in _errors_of(exc) if p not in before]
                    if not before or new:
                        _restore(path, previous)
                        raise HTTPException(422, {"errors": [
                            "the configuration does not work with this value — it was not changed",
                            *(new or _errors_of(exc)),
                        ]}) from exc
            _log.info("Config: secret %r set from the UI", name)
            return {"ok": True}

    @api.delete("/api/config/secrets/{name}")
    async def delete_config_secret(name: str) -> dict:
        """Remove a secret from secrets.yaml (only when nothing uses it)."""
        async with edit_lock:
            used_by = _read().secret_refs().get(name)
            if used_by:
                raise HTTPException(409, {"errors": [
                    f"secret {name!r} is still used by {', '.join(used_by)}"
                ]})
            try:
                _secrets().delete(name)
            except ConfigStoreError as exc:
                raise HTTPException(404, {"errors": [str(exc)]}) from exc
            _log.info("Config: secret %r removed from the UI", name)
            return {"ok": True}

    @api.delete("/api/config/{collection}/{entry_id}")
    async def delete_config_entry(collection: str, entry_id: str, revision: str | None = None) -> dict:
        """Remove one device, tariff or forecast from config.yaml and apply.
        A device's stored data (history, plans, learned models) stays."""
        if collection not in ("devices", *COLLECTIONS):
            raise HTTPException(404, {"errors": [f"unknown collection {collection!r}"]})
        async with edit_lock:
            doc = _read()
            try:
                new_text = doc.without_entry(collection, entry_id)
            except ConfigStoreError as exc:
                raise HTTPException(404, {"errors": [str(exc)]}) from exc
            result = await _apply(new_text, doc, revision)
            _log.info("Config: %s %r removed from the UI", collection, entry_id)
            return result

    @api.post("/api/config/reload")
    async def reload_config() -> dict:
        """Apply config.yaml as it is on disk, without a restart."""
        async with edit_lock:
            try:
                await app.reload_config()
            except Exception as exc:
                raise HTTPException(422, {"errors": _errors_of(exc)}) from exc
            return {"ok": True, "revision": _store().revision()}


def _restore(path: Any, previous: str | None) -> None:
    """Put secrets.yaml back as it was."""
    if previous is None:
        path.unlink(missing_ok=True)
        return
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(previous)
