# Energy Assistant: development notes

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
