# Release model

This document describes the release strategy for Energy Assistant, including
how versions are managed, images are published, and release notes are produced.

---

## Version source of truth

The canonical application version lives in **`pyproject.toml`** under
`[project] version`.  All other artefacts (container images, HA add-on
metadata) derive their version from git tags that must match this value.

Current version: `0.1.0`

---

## Release channels

| Channel   | Git trigger                         | Image tag(s)              | Intended audience            |
|-----------|-------------------------------------|---------------------------|------------------------------|
| **dev**   | push to `main`                      | `:dev`, `:sha-<short-sha>`| Developers; nightly testers  |
| **edge**  | prerelease tag `vX.Y.Z-rc.N` etc.   | `:edge`, `:<tag>`         | Early adopters               |
| **prod**  | stable tag `vX.Y.Z`                 | `:latest`, `:<tag>`       | All users                    |

Images are published to **GHCR**:
```
ghcr.io/cyberdns/energy-assistant:<tag>
```

---

## Workflow overview

```
main branch ──────────────────────────────────────────────► :dev, :sha-<sha>
                                                             (every push)

prerelease tag (v0.2.0-rc.1) ────────────────────────────► :edge, :v0.2.0-rc.1

stable tag (v0.2.0) ─────────────────────────────────────► :latest, :v0.2.0
```

The `.github/workflows/docker-publish.yml` workflow handles all three paths
automatically based on the git ref that triggered it.

---

## Making a release

### Dev (automatic)
Every push to `main` publishes a fresh `:dev` image.  No manual steps needed.

### Edge release
1. Ensure `pyproject.toml` version matches the planned release (e.g. `0.2.0`).
2. Create and push a prerelease tag:
   ```bash
   git tag v0.2.0-rc.1
   git push origin v0.2.0-rc.1
   ```
3. The workflow publishes `:edge` and `:v0.2.0-rc.1`.
4. Create a **GitHub pre-release** for the tag with release notes (see below).

### Stable release
1. Set `pyproject.toml` version to the final version (e.g. `0.2.0`).
2. Commit: `chore(release): bump version to 0.2.0`
3. Create and push a stable tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
4. The workflow publishes `:latest` and `:v0.2.0`.
5. Publish the **GitHub Release** with user-facing release notes.

---

## Release notes strategy

### dev channel
- No formal release notes are required.
- Each commit to `main` is visible in the commit log.
- GitHub's auto-generated release notes (if a release is created) are
  sufficient for tracking what changed.

### edge channel (pre-release tags)
- Create a **GitHub pre-release** for the tag.
- Use GitHub's **"Generate release notes"** button to auto-populate from merged
  PRs since the last tag.
- Edit the generated notes to highlight user-facing changes.
- Mark the release as **Pre-release** in the GitHub UI.

### prod channel (stable tags)
- Create a **GitHub Release** for the tag.
- Write user-facing release notes covering:
  - New features (what users can do now)
  - Breaking changes and migration steps
  - Bug fixes worth highlighting
- Use the `CHANGELOG.md` in the HA add-on folder as the
  human-readable summary visible in the Home Assistant UI.

### Canonical source
**GitHub Releases** are the canonical source of release notes for tagged
releases.  The HA add-on repository `CHANGELOG.md` files are a condensed
mirror intended for users who read them inside Home Assistant.

---

## Home Assistant add-on versioning

The HA add-on repository (`CyberDNS/hassio-addons-repository`) mirrors the
application channel structure:

| Add-on folder              | Tracks              | `config.json` version |
|----------------------------|---------------------|-----------------------|
| `energy-assistant-dev/`    | `:dev` image        | `0.1.0-dev`           |
| `energy-assistant-edge/`   | `:edge` image       | version from edge tag |
| `energy-assistant/`        | `:latest` image     | version from stable tag|

### Version format in `config.json`

- dev:   `0.1.0-dev`  (static; communicates "unstable snapshot")
- edge:  `0.2.0-rc1`  (mirrors the prerelease tag, dashes only — HA safe)
- prod:  `0.2.0`      (exact stable version)

### How to update the add-on version on a new release

When a new stable tag is pushed, update the HA add-on repository:
1. Set `config.json` `"version"` to match the new tag (without the `v` prefix).
2. Update `CHANGELOG.md` with a summary of user-visible changes.
3. Open a PR in `CyberDNS/hassio-addons-repository`.

This keeps the two repositories in sync without coupling their pipelines.
A future automation step can open that PR automatically from the
`docker-publish` workflow using a repository dispatch event.

---

## Version drift prevention

- The `pyproject.toml` version is the single source of truth.
- Git tags must match the `pyproject.toml` version (enforced by convention).
- The HA add-on `config.json` version must be updated alongside each release.
- Semver is used throughout: `MAJOR.MINOR.PATCH[-prerelease]`.
