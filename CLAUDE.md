# Nornwatt: development notes

## Python environment
- **Python 3.14**, the same version as CI (`.github/workflows/tests.yml`) and the
  container image (`Dockerfile`, `PYTHON_VERSION=3.14`). `.python-version` pins it,
  so `uv` selects it automatically.
- Do not use 3.15 (or another newer interpreter that happens to be installed):
  some dependencies have no prebuilt wheels for it yet (e.g. `rpds-py`, which then
  needs a Rust toolchain to build), and that's not what runs in production.
- Set up or refresh the venv (also in a fresh git worktree; venvs are not shared
  between worktrees):

  ```bash
  uv sync --extra dev        # installs the project + pytest from uv.lock
  ```

  `pytest` is in the `dev` *extra* (`[project.optional-dependencies]`), not in the
  `dev` dependency group, so a plain `uv sync` doesn't install it.

## Tests and lint
```bash
uv run pytest              # full suite, as CI runs it
uvx ruff check src tests   # lint (ruff isn't a project dependency)
```
New or changed code should pass `ruff check` cleanly.

## Web UI (`frontend/`)
- Vue 3 + Vite + Tailwind + shadcn-vue; needs Node 24. The build goes to
  `src/nornwatt/frontend/` (gitignored), which is what the server serves.
- A fresh worktree has no build: `cd frontend && npm ci && npm run build`.
  Without it `/` only says how to build, and the browser tests skip.

```bash
cd frontend
npm run build              # vue-tsc + vite build
npm test                   # vitest
uv run --with playwright pytest tests/e2e   # browser tests against the build (from the repo root)
```

## Config conventions
- **Power and energy from Home Assistant always offer the single/split switch.**
  Wherever the config takes a power or energy value from Home Assistant (a
  capability binding, `history:`, anything new), accept both forms — one signed
  entity (`entity`) or one entity per direction (`import_entity` /
  `export_entity`) — and build its form from
  `plugins/_homeassistant/bindings.binding_specs()` so the UI shows the same
  "Read from: one entity / separate …" switch (the `source` selector is UI-only,
  never in the file). Name the directions for the device (`CHARGE_DISCHARGE` for
  batteries and cars, `IMPORT_EXPORT` otherwise). Many integrations only offer
  the split pair (e.g. SMA: battery charge and discharge power), and a split pair
  keeps both directions of an hour apart.

## Documentation (CyberDNS/nornwatt.com)
- The user documentation is a separate repository, `CyberDNS/nornwatt.com` (checkout: `~/repos/nornwatt.com`),
  with one branch per channel: `main` (dev), `edge`, `stable`. Its README has the rules.
- **A change a user can see needs a documentation change in the same task**: a new device type, preset, tariff or
  forecast; a new or renamed setting, a changed default; changed behaviour (control, fail-safe, savings, learning);
  a new or changed page of the UI. Make it on a branch of the same name against the documentation's `main`, and
  link the two pull requests. Say "not needed" with the reason in the pull request when there is nothing to
  document (refactoring, tests, internal fixes).
- Settings tables are built from the configuration schema, so labels, descriptions and defaults in the plugin and
  category specs are user documentation: write them for a user (`label=`, `description=`). `scripts/export_docs_schema.py`
  exports the schema; `.github/workflows/sync-docs.yml` sends it to the documentation on every push to `main`, and the
  documentation's build fails when a type or preset has no page.
- To check before pushing: `uv run python scripts/export_docs_schema.py > ~/repos/nornwatt.com/src/data/schema.json`
  (in a worktree of the documentation), then `npm run check` there.
- Promotion follows the release: publishing a release opens the documentation's `main` → `edge` pull request,
  "Promote to prod" opens `edge` → `stable` for that version.
