# Release model

This document describes the release strategy for Energy Assistant, including
how versions are managed, images are published, and release notes are produced.

---

## Version source of truth

The canonical application version lives in **`pyproject.toml`** under
`[project] version`. Version tags (`vX.Y.Z`) must match this value.

Current version: `0.1.0`

There is no separate prerelease tag format anymore — every tag is a normal
release. What used to be "prerelease vs. stable" is now "edge vs. promoted
to prod": every tag lands on edge automatically, and a maintainer decides
later whether/when to promote that exact version to prod.

---

## Release channels

| Channel   | How a version gets there                                  | Image tag(s)               | Intended audience            |
|-----------|-------------------------------------------------------------|-----------------------------|-------------------------------|
| **dev**   | every push to `main`                                        | `:dev`, `:sha-<short-sha>` | Developers; nightly testers  |
| **edge**  | tag `vX.Y.Z` pushed, GitHub Release published                | `:edge`, `:<tag>`          | Early adopters                |
| **prod**  | a maintainer manually promotes an already-published edge version | `:latest` (retagged, not rebuilt) | All users |

Images are published to **GHCR**:
```
ghcr.io/cyberdns/energy-assistant:<tag>
```

---

## Workflow overview

```
main branch ──────────────────────────────────────────────► :dev, :sha-<sha>
                                                             (every push)

tag v0.2.0 pushed ─────► draft GitHub Release (auto-generated notes)
                              │
                    maintainer rewrites notes,
                        clicks "Publish"
                              │
                              ▼
                    release: published event ─────────────► :edge, :v0.2.0
                                                              PR opens in
                                                              energy-assistant-edge

    (some time later, once v0.2.0 has proven stable on edge)
                              │
                              ▼
      maintainer runs "Promote to prod" in hassio-addons-repository
                              │
                              ▼
                    :latest retagged to v0.2.0 (no rebuild)
                    PR opens in energy-assistant (prod folder)
```

- `.github/workflows/docker-publish.yml` builds and pushes images on `main`
  push and tag push.
- `.github/workflows/release.yml` creates a **draft** GitHub Release on tag
  push.
- `.github/workflows/sync-hassio-addons.yml` opens the dev PR on `main` push,
  and the edge PR when a release is **published** (not on tag push — this
  gives the curation step in between a chance to happen first).
- Promotion to prod lives entirely in `CyberDNS/hassio-addons-repository`
  (`promote-to-prod.yml`), not in this repo.

---

## Automated workflows

| Workflow | File | Triggered by |
|---|---|---|
| Tests | `tests.yml` | every PR and `main` push |
| Publish Docker image | `docker-publish.yml` | every PR (build-only), `main` push, and tag push |
| Create release | `release.yml` | tag push `v*` (creates a draft) |
| Sync HA add-on repo | `sync-hassio-addons.yml` | `main` push, and `release: published` |

### Required secrets

| Secret | Used by | Purpose |
|---|---|---|
| `GITHUB_TOKEN` | all workflows in this repo | default; provided automatically |
| `HASSIO_ADDONS_PAT` | `sync-hassio-addons.yml` | push a branch and open a PR in `CyberDNS/hassio-addons-repository` |

`HASSIO_ADDONS_PAT` must be a fine-grained PAT (or classic PAT) with:
- **Contents: Read & Write** on `CyberDNS/hassio-addons-repository`
- **Pull requests: Read & Write** on `CyberDNS/hassio-addons-repository`

The prod-promotion PAT (`GHCR_PROMOTE_PAT`, needs `write:packages` to retag
this repo's GHCR image) lives in `CyberDNS/hassio-addons-repository`, not here.

---

## Making a release

### Dev (automatic)
Every push to `main`:
- publishes a fresh `:dev` Docker image and `:sha-<short-sha>`,
- opens a PR in `CyberDNS/hassio-addons-repository` updating
  `energy-assistant-dev/config.yaml` and `CHANGELOG.md`.

No manual steps needed.

### Edge release
1. Ensure `pyproject.toml` version matches the planned release (e.g. `0.2.0`).
2. Commit: `chore(release): bump version to 0.2.0`
3. Create and push a tag:
   ```bash
   git tag v0.2.0
   git push origin v0.2.0
   ```
4. Automation publishes `:edge` and `:v0.2.0` Docker images, and creates a
   **draft** GitHub Release with auto-generated notes.
5. Rewrite the release body in the GitHub Releases UI into user-friendly
   language, then **publish** it.
6. Publishing triggers a PR in `CyberDNS/hassio-addons-repository` updating
   `energy-assistant-edge/` with the version and the curated notes.

### Promoting to prod
Once a version has been running on edge long enough to be considered stable:
1. Go to `CyberDNS/hassio-addons-repository` → Actions → **Promote to prod**.
2. Run it with `addon: energy-assistant` and the version to promote (e.g.
   `0.2.0`) — it must be the version currently on edge.
3. Automation retags `:latest` in GHCR to point at the exact same image
   (no rebuild) and opens a PR updating `energy-assistant/` (prod) with the
   version and changelog entry copied from edge.

---

## Release notes strategy

### dev channel
- No formal release notes are required.
- Each commit to `main` is visible in the commit log.
- The sync workflow adds a brief entry in the HA add-on
  `energy-assistant-dev/CHANGELOG.md` pointing to the commit.

### edge channel (tags)
- `release.yml` creates a **draft** GitHub Release with notes generated from
  merged PRs since the previous tag — a starting point, not the final text.
- A maintainer rewrites the body into user-friendly language and publishes
  the release. Auto-generated commit/PR-title dumps never reach end users.
- Publishing triggers the sync to `energy-assistant-edge/CHANGELOG.md`,
  which copies the curated release body verbatim.

### prod channel (promoted versions)
- No new release notes are written — the prod changelog entry is copied
  from the edge entry that was already curated for that version.

### Canonical source
**GitHub Releases** are the canonical source of release notes. The HA add-on
repository `CHANGELOG.md` files are a condensed mirror intended for users who
read them inside Home Assistant, and must not become an independent source
of truth.

---

## Home Assistant add-on versioning

The HA add-on repository (`CyberDNS/hassio-addons-repository`) mirrors the
application channel structure:

| Add-on folder              | Tracks                          | `config.yaml` version   |
|-----------------------------|----------------------------------|--------------------------|
| `energy-assistant-dev/`    | `:dev` image                     | `0.1.0-dev`              |
| `energy-assistant-edge/`   | `:edge` image                    | version from the tag     |
| `energy-assistant/`        | `:latest` image (promoted)       | version last promoted    |

### How add-on versions are updated

- `sync-hassio-addons.yml` (in this repo) handles dev and edge automatically.
- `promote-to-prod.yml` (in `hassio-addons-repository`) handles prod, on
  manual trigger only.

Manual edits are only needed if the automation is bypassed or a PR needs
human editing before merging.

---

## Version drift prevention

- The `pyproject.toml` version is the single source of truth.
- Git tags must match the `pyproject.toml` version (enforced by convention).
- Semver is used throughout: `MAJOR.MINOR.PATCH`. Prerelease suffixes are no
  longer part of the tagging convention — edge/prod is a promotion decision,
  not a tag format.
