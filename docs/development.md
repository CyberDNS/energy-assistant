# Development

See [ARCHITECTURE.md](../ARCHITECTURE.md) for the technical design,
[DOMAIN_MODEL.md](../DOMAIN_MODEL.md) for the device model and
[RELEASE.md](../RELEASE.md) for releases.

## Backend

Python 3.14 with [uv](https://docs.astral.sh/uv/):

```bash
uv sync --extra dev        # the project and pytest
uv run pytest              # the test suite, as CI runs it
uvx ruff check src tests   # lint
uv run nornwatt config.yaml --db data/history.db
```

## Developing the web UI

The UI lives in `frontend/` (Vue 3, Vite, Tailwind, shadcn-vue components in
`frontend/src/components/ui/`) and needs Node 24:

```bash
cd frontend
npm ci
npm run build        # type check + build into src/nornwatt/frontend/, which the server serves
npm test             # unit tests
NORNWATT_API=http://localhost:8088 npm run dev   # dev server with hot reload, API calls go to a running Nornwatt
```

The build output is not in git; the container image builds it (`Dockerfile`),
and a checkout without it serves a page that says how to build it. The sidebar
is defined in `frontend/src/nav.ts`, the settings categories in
`frontend/src/settings.ts`.
