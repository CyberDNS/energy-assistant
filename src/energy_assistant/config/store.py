"""DeviceConfigStore — edits the ``devices:`` section of config.yaml in place.

The configuration UI changes one device at a time.  ``config.yaml`` stays the
single source of truth and stays hand-editable: an edit rewrites only the
lines of the touched device entry; every other line of the file — comments,
blank lines, formatting, the other sections — is kept byte for byte.  Inside
the touched entry, comments and ``!secret`` tags of unchanged keys are kept.

Workflow (see ``server``: ``PUT /api/config/devices/{id}``)::

    store = DeviceConfigStore(path)
    doc = store.read()                       # text + revision + device entries
    new_text = doc.with_device("car", entry) # nothing written yet
    ...check new_text with YamlConfigLoader.load_text + validate_plugins...
    store.write(new_text, expected_revision=doc.revision)

Device entries are exchanged in *structured* form (``entry_to_form`` /
``form_to_entry``): the model keys of ``core/device_model`` plus the plugin's
own settings under ``settings``.  Values that are ``!secret name`` in the file
appear as ``{"!secret": "name"}``; settings declared ``secret`` that hold a
plain value are never handed out (``{"!hidden": true}``, which on the way back
means "keep what is in the file").

``write`` keeps a timestamped copy of the previous file in ``config_backups/``
next to it (the newest ``KEEP_BACKUPS``).
"""

from __future__ import annotations

import hashlib
import io
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq, TaggedScalar

from ..core.device_model import MODEL_KEYS, ConnectionType, ParamSpec

SECRET = "!secret"
HIDDEN = "!hidden"
KEEP_BACKUPS = 20
BACKUP_DIR = "config_backups"

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SECRET_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

#: Key order of a newly written entry (plugin settings follow ``tariff``).
_HEAD_KEYS = ("id", "category", "label", "type", "tariff")
_TAIL_KEYS = ("capabilities", "params", *(c.value for c in ConnectionType),
              "forecast", "learning", "ha_discovery", "energy_dashboard")


class ConfigStoreError(ValueError):
    """The config file cannot be edited as asked."""


class ConfigConflictError(ConfigStoreError):
    """The file changed since it was read."""


def revision_of(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


#: How lists are written: "indented" puts the dashes two columns right of
#: the key above them, "flush" directly below it.  Both are common; an edit
#: uses the style the entry already has (see ``ConfigDocument._first_valid``).
STYLES = ("indented", "flush")


def _yaml(style: str = "indented") -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    if style == "flush":
        y.indent(mapping=2, sequence=2, offset=0)
    else:
        y.indent(mapping=2, sequence=4, offset=2)
    return y


def _plain(node: Any) -> Any:
    """ruamel node → plain Python data; ``!secret x`` → ``{"!secret": "x"}``."""
    if isinstance(node, TaggedScalar):
        if node.tag.value == SECRET:
            return {SECRET: str(node.value)}
        return node.value
    if isinstance(node, Mapping):
        return {str(k): _plain(v) for k, v in node.items()}
    if isinstance(node, (list, tuple)):
        return [_plain(v) for v in node]
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, int):
        return int(node)
    if isinstance(node, float):
        return float(node)
    if isinstance(node, str):
        return str(node)
    return node


def _node(value: Any) -> Any:
    """Plain data → ruamel node (``{"!secret": "x"}`` → ``!secret x``)."""
    if isinstance(value, Mapping):
        if set(value) == {SECRET}:
            return TaggedScalar(value=str(value[SECRET]), style=None, tag=SECRET)
        out = CommentedMap()
        for k, v in value.items():
            out[k] = _node(v)
        return out
    if isinstance(value, (list, tuple)):
        seq = CommentedSeq(_node(v) for v in value)
        # Short lists of scalars read best inline: [a, b, c]
        if all(not isinstance(v, (Mapping, list, tuple)) for v in value):
            seq.fa.set_flow_style()
        return seq
    return value


def _merge(node: CommentedMap, new: Mapping[str, Any]) -> None:
    """Make *node* equal to *new*, touching only what differs (keeps the
    comments and formatting of unchanged keys)."""
    for key in [k for k in node if k not in new]:
        del node[key]
    for key, value in new.items():
        if key in node:
            if _plain(node[key]) == value:
                continue
            if isinstance(node[key], CommentedMap) and isinstance(value, Mapping) \
                    and set(value) != {SECRET}:
                _merge(node[key], value)
                continue
            if isinstance(node[key], CommentedSeq) and isinstance(value, (list, tuple)) and value:
                _merge_seq(node[key], value)
                continue
        node[key] = _node(value)


def _merge_seq(node: CommentedSeq, new: list[Any] | tuple[Any, ...]) -> None:
    """Make the list *node* equal to *new*, keeping unchanged items (and
    their comments) in place."""
    for i, value in enumerate(new):
        if i >= len(node):
            node.append(_node(value))
        elif _plain(node[i]) == value:
            continue
        elif isinstance(node[i], CommentedMap) and isinstance(value, Mapping) \
                and set(value) != {SECRET}:
            _merge(node[i], value)
        else:
            node[i] = _node(value)
    while len(node) > len(new):
        node.pop()


def _drop_trailing_comments(node: Any) -> None:
    """Remove the comment lines that follow *node* from its ruamel comment
    attributes (an end-of-line comment on its last line stays).

    ruamel attaches the comments after a block to the block's last leaf.  Those
    lines stay in the file outside the block that is rewritten, so without
    this they would be written twice, or end up in the middle of the block when
    a key is appended.
    """
    while isinstance(node, (CommentedMap, CommentedSeq)) and len(node):
        last = next(reversed(node)) if isinstance(node, CommentedMap) else len(node) - 1
        if getattr(node.ca, "end", None):
            del node.ca.end[:]  # whole comment lines after a nested block
        for token in node.ca.items.get(last) or []:
            for tok in token if isinstance(token, list) else [token]:
                if tok is not None and "\n" in tok.value:
                    head = tok.value.split("\n", 1)[0]
                    tok.value = head + "\n" if head.strip() else "\n"
        node = node[last]


def _is_filler(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


def _ordered(entry: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: entry[k] for k in _HEAD_KEYS if k in entry}
    out.update({k: v for k, v in entry.items() if k not in _HEAD_KEYS and k not in _TAIL_KEYS})
    out.update({k: entry[k] for k in _TAIL_KEYS if k in entry})
    return out


# ---------------------------------------------------------------------------
# Structured form  <->  raw entry
# ---------------------------------------------------------------------------


def mask_secrets(values: Any, specs: Mapping[str, ParamSpec] | None) -> Any:
    """*values* (a mapping checked by *specs*) with every plain value of a
    ``secret`` setting replaced by ``{"!hidden": true}``, at any depth."""
    if not isinstance(values, Mapping) or not specs:
        return values
    out = dict(values)
    for name, value in values.items():
        spec = specs.get(name)
        if spec is None or value is None:
            continue
        if spec.secret:
            if not _is_secret_ref(value):
                out[name] = {HIDDEN: True}
        elif spec.fields is not None:
            out[name] = mask_secrets(value, spec.fields)
    return out


def unmask_secrets(values: Any, existing: Any, where: str = "") -> Any:
    """*values* with every ``{"!hidden": true}`` replaced by the value at the
    same place in *existing* (what is in the file)."""
    if isinstance(values, Mapping):
        if set(values) == {HIDDEN}:
            if existing is None:
                raise ConfigStoreError(f"{where or 'value'}: no stored value to keep")
            return existing
        old = existing if isinstance(existing, Mapping) else {}
        return {k: unmask_secrets(v, old.get(k), f"{where}.{k}" if where else str(k))
                for k, v in values.items()}
    if isinstance(values, list):
        old_list = existing if isinstance(existing, list) else []
        return [unmask_secrets(v, old_list[i] if i < len(old_list) else None, where)
                for i, v in enumerate(values)]
    return values


def prune_empty(value: Any) -> Any:
    """*value* without keys whose value is empty (None, "", {}, [])."""
    if isinstance(value, Mapping):
        if set(value) in ({SECRET}, {HIDDEN}):
            return dict(value)
        out = {k: prune_empty(v) for k, v in value.items()}
        return {k: v for k, v in out.items() if v is not None and v != "" and v != {} and v != []}
    if isinstance(value, list):
        return [prune_empty(v) for v in value]
    return value


def entry_to_form(
    device_id: str,
    entry: Mapping[str, Any],
    setting_specs: Mapping[str, ParamSpec] | None = None,
) -> dict[str, Any]:
    """The structured form of a raw device entry (see module docstring)."""
    entry = dict(entry)
    if isinstance(entry.get("source"), Mapping):  # legacy nested plugin settings
        entry.update(entry.pop("source"))
    settings = mask_secrets({k: v for k, v in entry.items() if k not in MODEL_KEYS},
                            setting_specs)
    form: dict[str, Any] = {
        "id": device_id,
        "category": entry.get("category"),
        "type": entry.get("type"),
        "label": entry.get("label"),
        "tariff": entry.get("tariff"),
        "settings": settings,
        "capabilities": entry.get("capabilities") or {},
        "params": entry.get("params") or {},
    }
    for conn in ConnectionType:
        form[conn.value] = entry.get(conn.value)
    form["forecast"] = entry.get("forecast")
    form["learning"] = entry.get("learning", True)
    form["ha_discovery"] = entry.get("ha_discovery", True) is not False
    if entry.get("energy_dashboard"):
        form["energy_dashboard"] = entry["energy_dashboard"]
    return form


def form_to_entry(form: Mapping[str, Any], existing: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The raw entry (without ``id``) for a structured *form*.

    Empty values are left out.  ``{"!hidden": true}`` settings take their
    value from *existing* (the entry as it is in the file).
    """
    existing = dict(existing or {})
    if isinstance(existing.get("source"), Mapping):
        existing.update(existing.pop("source"))
    entry: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is None or value == "" or value == {} or value == []:
            return
        entry[key] = value

    put("category", form.get("category"))
    put("label", form.get("label"))
    put("type", form.get("type"))
    put("tariff", form.get("tariff"))
    settings = form.get("settings") or {}
    if not isinstance(settings, Mapping):
        raise ConfigStoreError("'settings' must be a mapping")
    for name, value in settings.items():
        if name in MODEL_KEYS or name == "source":
            raise ConfigStoreError(f"{name!r} is not a plugin setting")
        put(name, prune_empty(unmask_secrets(value, existing.get(name), f"setting {name!r}")))
    put("capabilities", prune_empty(form.get("capabilities") or {}))
    put("params", prune_empty(form.get("params") or {}))
    for conn in ConnectionType:
        put(conn.value, form.get(conn.value))
    put("forecast", prune_empty(form.get("forecast")))
    learning = form.get("learning", True)
    if learning is False:
        entry["learning"] = False
    elif isinstance(learning, Mapping) and (options := prune_empty(learning)):
        entry["learning"] = options
    if form.get("ha_discovery") is False:
        entry["ha_discovery"] = False
    put("energy_dashboard", form.get("energy_dashboard"))
    return entry


def _is_secret_ref(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == {SECRET}


# ---------------------------------------------------------------------------
# Document
# ---------------------------------------------------------------------------


@dataclass
class _Span:
    start: int   # first line of the entry (0-based)
    end: int     # one past its last content line
    indent: int  # column of the "-" (list) or of the key (mapping)
    tail: int    # one past the comment lines right below that belong to it


class _Collection:
    """The entries of one section that holds entries keyed by id
    (``devices``, ``tariffs``, ``forecasts``), in list or mapping style."""

    def __init__(self, doc: ConfigDocument, name: str) -> None:
        self.name = name
        node = doc._doc.get(name)
        self.present = name in doc._doc
        if node is None:
            node = CommentedSeq() if name == "devices" else CommentedMap()
        if not isinstance(node, (CommentedSeq, CommentedMap)):
            raise ConfigStoreError(f"'{name}:' must be a list or a mapping")
        self.node = node
        self.is_list = isinstance(node, CommentedSeq)
        self.nodes: dict[str, CommentedMap] = {}
        self.spans: dict[str, _Span] = {}
        lines = doc._lines
        starts: list[tuple[str, int, int, CommentedMap]] = []
        if self.is_list:
            for i, item in enumerate(node):
                if not isinstance(item, CommentedMap) or not item.get("id"):
                    continue
                line, col = node.lc.item(i)
                # lc is the position of the item's content; the dash is before it.
                dash = lines[line].find("-")
                starts.append((str(item["id"]), line, dash if 0 <= dash < col else col, item))
        else:
            for key, item in node.items():
                if not isinstance(item, CommentedMap):
                    continue
                line, col = node.lc.key(key)
                starts.append((str(key), line, col, item))
        section_end = doc._section_span(name)[1] if self.present else len(lines)
        for n, (entry_id, line, indent, item) in enumerate(starts):
            limit = starts[n + 1][1] if n + 1 < len(starts) else section_end
            end = limit
            while end > line + 1 and _is_filler(lines[end - 1]):
                end -= 1
            # Comments right below, indented deeper than the entry, are part
            # of it (a commented-out key); other comments belong to what follows.
            tail = end
            while tail < limit and lines[tail].strip().startswith("#") and (
                len(lines[tail]) - len(lines[tail].lstrip()) > indent
            ):
                tail += 1
            if entry_id in self.nodes:
                raise ConfigStoreError(f"{name}: id {entry_id!r} appears twice in the config file")
            self.nodes[entry_id] = item
            self.spans[entry_id] = _Span(line, end, indent, tail)


class ConfigDocument:
    """A parsed config file whose parts can be replaced.

    The file has *collections* — sections of entries keyed by id
    (``devices``, ``tariffs``, ``forecasts``; ``entry`` / ``with_entry`` /
    ``without_entry``) — and *settings sections* that are one mapping
    (``backends``, ``optimizer``, …; ``section`` / ``with_section``).  The
    ``with…`` methods return the new file text; nothing is written.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.revision = revision_of(text)
        self._lines = text.splitlines(keepends=True)
        try:
            self._doc = _yaml().load(text) or CommentedMap()
        except Exception as exc:
            raise ConfigStoreError(f"config file is not valid YAML: {exc}") from exc
        if not isinstance(self._doc, CommentedMap):
            raise ConfigStoreError("config file must be a mapping of sections")
        self._collections: dict[str, _Collection] = {}

    # -- reading -----------------------------------------------------------

    def _collection(self, name: str) -> _Collection:
        if name not in self._collections:
            self._collections[name] = _Collection(self, name)
        return self._collections[name]

    def _section_span(self, name: str) -> tuple[int, int]:
        """Lines ``[start, end)`` of the top-level section *name*, without the
        blank lines and comments that precede the next section."""
        keys = list(self._doc)
        idx = keys.index(name)
        start = self._doc.lc.key(name)[0]
        end = self._doc.lc.key(keys[idx + 1])[0] if idx + 1 < len(keys) else len(self._lines)
        while end > start + 1 and _is_filler(self._lines[end - 1]):
            end -= 1
        return start, end

    def ids(self, collection: str) -> list[str]:
        return list(self._collection(collection).nodes)

    def entry(self, collection: str, entry_id: str | None = None) -> dict[str, Any] | None:
        """The raw entry as plain data (without ``id``); None when unknown.

        ``entry(device_id)`` is short for ``entry("devices", device_id)``.
        """
        if entry_id is None:
            collection, entry_id = "devices", collection
        coll = self._collection(collection)
        node = coll.nodes.get(entry_id)
        if node is None:
            return None
        return {k: v for k, v in _plain(node).items() if not (coll.is_list and k == "id")}

    def entries(self, collection: str = "devices") -> dict[str, dict[str, Any]]:
        return {entry_id: self.entry(collection, entry_id) or {} for entry_id in self.ids(collection)}

    def section(self, name: str) -> dict[str, Any]:
        """A settings section as plain data ({} when absent)."""
        value = _plain(self._doc.get(name))
        return value if isinstance(value, dict) else {}

    def top_level(self, section: str) -> Any:
        return _plain(self._doc.get(section))

    def secret_refs(self) -> dict[str, list[str]]:
        """Where each ``!secret`` name is used: name → paths like
        ``tariffs.household.token``."""
        refs: dict[str, list[str]] = {}

        def walk(node: Any, path: str) -> None:
            if isinstance(node, TaggedScalar):
                if node.tag.value == SECRET:
                    refs.setdefault(str(node.value), []).append(path)
            elif isinstance(node, Mapping):
                for key, value in node.items():
                    walk(value, f"{path}.{key}" if path else str(key))
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    label = value.get("id") if isinstance(value, Mapping) else None
                    walk(value, f"{path}.{label or i + 1}")

        walk(self._doc, "")
        return refs

    # Device shortcuts.
    def device_ids(self) -> list[str]:
        return self.ids("devices")

    def with_device(self, device_id: str, entry: Mapping[str, Any]) -> str:
        return self.with_entry("devices", device_id, entry)

    def without_device(self, device_id: str) -> str:
        return self.without_entry("devices", device_id)

    # -- editing (returns the new text; nothing is written) ------------------

    def with_entry(self, collection: str, entry_id: str, entry: Mapping[str, Any]) -> str:
        """The file text with *entry_id* of *collection* set to *entry*
        (added when new)."""
        coll = self._collection(collection)
        if entry_id in coll.nodes:
            new = self._replace(coll, entry_id, entry)
        else:
            if not _ID_RE.match(entry_id):
                raise ConfigStoreError(
                    f"id {entry_id!r}: use lower-case letters, digits and '_', "
                    "starting with a letter"
                )
            new = self._insert(coll, entry_id, entry)
        return self._verified(new, lambda doc: doc.entry(collection, entry_id) == _clean(entry),
                              f"{collection}: {entry_id}")

    def without_entry(self, collection: str, entry_id: str) -> str:
        coll = self._collection(collection)
        span = coll.spans.get(entry_id)
        if span is None:
            raise ConfigStoreError(f"{collection}: {entry_id!r} is not in the config file")
        lines = list(self._lines)
        start, end = span.start, span.tail
        # Take one neighbouring blank line along, so no double gap is left.
        is_last = entry_id == list(coll.nodes)[-1]
        if len(coll.nodes) == 1:
            pass  # the section head stays, with the gap after it
        elif is_last:
            # The gap after the last entry separates the section from the next.
            if start > 0 and not lines[start - 1].strip():
                start -= 1
        elif end < len(lines) and not lines[end].strip():
            end += 1
        del lines[start:end]
        if len(coll.nodes) == 1:
            # The section would be left as a bare 'name:' — make it explicit.
            head = self._doc.lc.key(collection)[0]
            lines[head] = f"{collection}: {'[]' if coll.is_list else '{}'}\n"
        return self._verified("".join(lines),
                              lambda doc: entry_id not in doc.ids(collection),
                              f"{collection}: {entry_id}")

    def with_section(self, name: str, values: Mapping[str, Any]) -> str:
        """The file text with the settings section *name* set to *values*
        (removed when *values* is empty)."""
        lines = list(self._lines)
        if name in self._doc:
            start, end = self._section_span(name)
            if not values:
                if end < len(lines) and not lines[end].strip():
                    end += 1
                del lines[start:end]
            else:
                node = self._doc[name]
                if not isinstance(node, CommentedMap):
                    node = CommentedMap()
                if _plain(node) == dict(values):
                    return self.text
                _drop_trailing_comments(node)
                _merge(node, values)
                original = self._lines[start:end]

                def candidate(style: str, reconcile: bool) -> str:
                    rendered = _dump({name: node}, style)
                    out = list(self._lines)
                    out[start:end] = _reconciled(rendered, original) if reconcile else rendered
                    return "".join(out)

                return self._first_valid(
                    candidate, lambda doc: doc.section(name) == _clean(values), name
                )
        elif values:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines += (["\n"] if lines else []) + _dump({name: _node(values)})
        return self._verified("".join(lines), lambda doc: doc.section(name) == _clean(values), name)

    def _first_valid(self, candidate: Any, check: Any, what: str) -> str:
        """The first rendering of a changed block that leaves a correct file.

        Unchanged lines keep their original form, so the new lines must have
        the same list style as the rest of the block: both styles are tried.
        If neither fits (mixed styles), the block is written afresh.
        """
        last: ConfigStoreError | None = None
        for style, reconcile in ((STYLES[0], True), (STYLES[1], True), (STYLES[0], False)):
            try:
                return self._verified(candidate(style, reconcile), check, what)
            except ConfigStoreError as exc:
                last = exc
        assert last is not None
        raise last

    def _verified(self, text: str, check: Any, what: str) -> str:
        """*text*, after making sure it parses and holds what was asked for."""
        try:
            ok = check(ConfigDocument(text))
        except ConfigStoreError:
            ok = False
        # An edit never adds comment lines; more of them means one was duplicated.
        if ok and _comment_lines(text) > _comment_lines(self.text):
            ok = False
        if not ok:
            raise ConfigStoreError(
                f"{what}: could not apply the change to config.yaml safely (unusual "
                "formatting) — edit the file by hand"
            )
        return text

    def _render(self, coll: _Collection, entry_id: str, node: Any, indent: int,
                style: str = "indented") -> list[str]:
        """*node* as the lines of an entry of *coll* at column *indent*."""
        if coll.is_list:
            out = _dump(CommentedSeq([node]), style)
            base = 0 if style == "flush" else 2  # column the dump puts the dash at
        else:
            out = _dump({entry_id: node}, style)
            base = 0
        return [
            (" " * indent + line[base:]) if line.strip() and not line[:base].strip() else line
            for line in out
        ]

    def _replace(self, coll: _Collection, entry_id: str, entry: Mapping[str, Any]) -> str:
        node, span = coll.nodes[entry_id], coll.spans[entry_id]
        if self.entry(coll.name, entry_id) == dict(entry):
            return self.text
        new = dict(entry)
        if coll.is_list:
            # Keep the position of 'id' (first key by convention).
            new = {"id": node.get("id", entry_id), **new}
        _drop_trailing_comments(node)
        _merge(node, new)
        original = self._lines[span.start:span.end]

        def candidate(style: str, reconcile: bool) -> str:
            rendered = self._render(coll, entry_id, node, span.indent, style)
            lines = list(self._lines)
            lines[span.start:span.end] = _reconciled(rendered, original) if reconcile else rendered
            return "".join(lines)

        return self._first_valid(
            candidate, lambda doc: doc.entry(coll.name, entry_id) == _clean(entry),
            f"{coll.name}: {entry_id}",
        )

    def _insert(self, coll: _Collection, entry_id: str, entry: Mapping[str, Any]) -> str:
        data = _ordered({"id": entry_id, **entry} if coll.is_list else dict(entry))
        lines = list(self._lines)
        if not coll.nodes:
            return self._insert_first(coll, entry_id, entry, lines)
        # After the last entry of the same category, else after the last entry.
        same = [d for d, n in coll.nodes.items()
                if entry.get("category") and n.get("category") == entry.get("category")]
        anchor = coll.spans[(same or list(coll.nodes))[-1]]
        block = self._render(coll, entry_id, _node(data), anchor.indent)
        # A blank line before the new entry when the entries are spaced that way.
        if coll.name == "devices" or (anchor.start > 0 and not lines[anchor.start - 1].strip()):
            block = ["\n", *block]
        if not lines[anchor.tail - 1].endswith("\n"):
            lines[anchor.tail - 1] += "\n"
        lines[anchor.tail:anchor.tail] = block
        return "".join(lines)

    def _insert_first(
        self, coll: _Collection, entry_id: str, entry: Mapping[str, Any], lines: list[str]
    ) -> str:
        name = coll.name
        as_list = name == "devices"
        coll.is_list = as_list
        data = _node(_ordered({"id": entry_id, **entry} if as_list else dict(entry)))
        block = [f"{name}:\n", *self._render(coll, entry_id, data, 2)]
        if coll.present:
            # The section exists but is empty ('name:', 'name: []', 'name: {}').
            start, end = self._section_span(name)
            lines[start:end] = block
            return "".join(lines)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        return "".join(lines + (["\n"] if lines else []) + block)


def _comment_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("#"))


def _clean(value: Any) -> Any:
    """Plain data as it reads back from the file (tuples → lists)."""
    if isinstance(value, Mapping):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def _dump(data: Any, style: str = "indented") -> list[str]:
    """*data* as YAML lines, without the comments that trail it (those stay
    where they are in the file)."""
    if isinstance(data, dict) and not isinstance(data, CommentedMap):
        wrapper = CommentedMap()
        for key, value in data.items():
            wrapper[key] = value
        data = wrapper
    buf = io.StringIO()
    _yaml(style).dump(data, buf)
    out = [line.rstrip() + "\n" for line in buf.getvalue().splitlines()]
    while out and _is_filler(out[-1]):
        out.pop()
    return out


def _reconciled(rendered: list[str], original: list[str]) -> list[str]:
    """*rendered*, with every line that only differs in spacing from a line of
    *original* replaced by that original line."""
    originals: dict[str, list[str]] = {}
    for line in original:
        originals.setdefault("".join(line.split()), []).append(line)
    out = []
    for line in rendered:
        queue = originals.get("".join(line.split()))
        if queue:
            line = queue.pop(0)
            line = line if line.endswith("\n") else line + "\n"
        out.append(line)
    return out


class DeviceConfigStore:
    """Reads and atomically writes the config file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def read(self) -> ConfigDocument:
        return ConfigDocument(self.path.read_text() if self.path.exists() else "")

    def revision(self) -> str:
        return revision_of(self.path.read_text() if self.path.exists() else "")

    def write(self, text: str, expected_revision: str | None = None) -> str:
        """Replace the file with *text*; returns the new revision.

        Raises ``ConfigConflictError`` when the file no longer has
        *expected_revision* (someone edited it in the meantime).
        """
        current = self.path.read_text() if self.path.exists() else ""
        if expected_revision is not None and revision_of(current) != expected_revision:
            raise ConfigConflictError(
                "config.yaml was changed in the meantime — reload the page and try again"
            )
        if text == current:
            return revision_of(current)
        if current:
            self._backup(current)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(text)
        if self.path.exists():
            os.chmod(tmp, self.path.stat().st_mode & 0o777)
        os.replace(tmp, self.path)
        return revision_of(text)

    def _backup(self, text: str) -> None:
        directory = self.path.parent / BACKUP_DIR
        directory.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = directory / f"{self.path.stem}-{stamp}{self.path.suffix}"
        target.write_text(text)
        os.chmod(target, 0o600)
        backups = sorted(directory.glob(f"{self.path.stem}-*{self.path.suffix}"))
        for old in backups[:-KEEP_BACKUPS]:
            old.unlink(missing_ok=True)


class SecretsStore:
    """Reads the names in ``secrets.yaml`` and sets or removes entries.

    Values are write-only here: they are never returned.  The file keeps its
    comments and order, is written atomically with mode 600, and the previous
    version is kept in ``config_backups/`` (mode 600, newest ``KEEP_BACKUPS``).
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _load(self) -> CommentedMap:
        if not self.path.exists():
            return CommentedMap()
        try:
            data = _yaml().load(self.path.read_text()) or CommentedMap()
        except Exception as exc:
            raise ConfigStoreError(f"secrets.yaml is not valid YAML: {exc}") from exc
        if not isinstance(data, CommentedMap):
            raise ConfigStoreError("secrets.yaml must be a mapping of name → value")
        return data

    def names(self) -> list[str]:
        return [str(name) for name in self._load()]

    def set(self, name: str, value: str) -> None:
        if not _SECRET_NAME_RE.match(name):
            raise ConfigStoreError(
                f"secret name {name!r}: use letters, digits, '_' and '-', starting with a letter"
            )
        if not isinstance(value, str) or not value:
            raise ConfigStoreError("a secret needs a value")
        data = self._load()
        data[name] = value
        self._write(data)

    def delete(self, name: str) -> None:
        data = self._load()
        if name not in data:
            raise ConfigStoreError(f"secret {name!r} is not in secrets.yaml")
        del data[name]
        self._write(data)

    def _write(self, data: CommentedMap) -> None:
        store = DeviceConfigStore(self.path)
        buf = io.StringIO()
        _yaml().dump(data, buf)
        current = self.path.read_text() if self.path.exists() else ""
        if current:
            store._backup(current)
        tmp = self.path.with_name(self.path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(buf.getvalue())
        os.replace(tmp, self.path)
