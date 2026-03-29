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

## Automated workflows

Three workflows handle the full release pipeline automatically:

| Workflow | File | Triggered by |
|---|---|---|
| Tests | `tests.yml` | every PR and `main` push |
| Publish Docker image | `docker-publish.yml` | every PR (build-only), `main` push, and tag push |
| Create release | `release.yml` | tag push `v*` |
| Sync HA add-on repo | `sync-hassio-addons.yml` | `main` push and tag push `v*` |

### Required secrets

| Secret | Used by | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | all workflows | default; provided automatically |
| `HASSIO_ADDONS_PAT` | `sync-hassio-addons.yml` | push a branch and open a PR in `CyberDNS/hassio-addons-repository` |

`HASSIO_ADDONS_PAT` must be a fine-grained PAT (or classic PAT) with:
- **Contents: Read & Write** on `CyberDNS/hassio-addons-repository`
- **Pull requests: Read & Write** on `CyberDNS/hassio-addons-repository`

---

## Making a release

### Dev (automatic)
Every push to `main`:
- publishes a fresh `:dev` Docker image and `:sha-<short-sha>`,
- opens a PR in `CyberDNS/hassio-addons-repository` updating
  `energy-assistant-dev/config.json` and `CHANGELOG.md`.

No manual steps needed.

### Edge release
1. Ensure `pyproject.toml` version matches the planned release (e.g. `0.2.0`).
2. Create and push a prerelease tag:
   ```bash
   git tag v0.2.0-rc.1
   git push origin v0.2.0-rc.1
   ```
3. Automation will:
   - publish `:edge` and `:v0.2.0-rc.1` Docker images,
   - create a GitHub **pre-release** with auto-generated notes,
   - open a PR in `CyberDNS/hassio-addons-repository` updating
     `energy-assistant-edge/`.
4. Optionally refine the release notes in the GitHub Releases UI.

### Stable release
1. Set `pyproject.toml` version to the final version (e.g. `0.2.0`).
2. Commit: `chore(release): bump version to 0.2.0`
3. Create and push a stable tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
4. Automation will:
   - publish `:latest` and `:v0.2.0` Docker images,
   - create a GitHub **release** with auto-generated notes,
   - open a PR in `CyberDNS/hassio-addons-repository` updating
     `energy-assistant/`.
5. Optionally refine the release notes in the GitHub Releases UI.

---

## Release notes strategy

### dev channel
- No formal release notes are required.
- Each commit to `main` is visible in the commit log.
- The sync workflow adds a brief entry in the HA add-on
  `energy-assistant-dev/CHANGELOG.md` pointing to the commit.

### edge channel (pre-release tags)
- The `release.yml` workflow automatically creates a **GitHub pre-release**
  with notes generated from merged PRs since the previous tag.
- Maintainers can refine the body in the GitHub Releases UI after the
  workflow runs.
- The HA add-on `energy-assistant-edge/CHANGELOG.md` is updated automatically
  with a short entry that links back to the upstream release.

### prod channel (stable tags)
- The `release.yml` workflow automatically creates a **GitHub release** with
  notes generated from merged PRs since the previous tag.
- Maintainers can refine the body in the GitHub Releases UI after the
  workflow runs.
- The HA add-on `energy-assistant/CHANGELOG.md` is updated automatically
  with a short entry that links back to the upstream release.

### Canonical source
**GitHub Releases** are the canonical source of release notes for tagged
releases.  The HA add-on repository `CHANGELOG.md` files are a condensed
mirror intended for users who read them inside Home Assistant.  The add-on
repo must not become an independent source of truth.

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
- edge:  `0.2.0-rc.1`  (mirrors the prerelease tag, without the leading `v`)
- prod:  `0.2.0`      (exact stable version)

### How add-on versions are updated

The `sync-hassio-addons.yml` workflow handles this automatically by opening a
PR in `CyberDNS/hassio-addons-repository` whenever a push to `main` or a tag
push occurs.  Manual updates are only needed if the automation is bypassed or
the PR needs human editing before merging.

---

## Version drift prevention

- The `pyproject.toml` version is the single source of truth.
- Git tags must match the `pyproject.toml` version (enforced by convention).
- The HA add-on `config.json` version must be updated alongside each release.
- Semver is used throughout: `MAJOR.MINOR.PATCH[-prerelease]`.
