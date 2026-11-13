# Dependency updates

This runbook is for agents and maintainers who review, land, add, hold, or stop
dependency updates. Renovate is Booley's only update bot: its configuration is
`.github/renovate.json`, and `.github/workflows/renovate.yml` runs it every 4
hours and on `workflow_dispatch`. `tests/ci/test_renovate_config.py` pins that
configuration and parts of this page. Landing a PR follows
[merge-queue.md](merge-queue.md).

## How updates arrive

Schedules are UTC days; the first Renovate run of the day opens the PR.

| Group | Schedule | Touches | Approval |
|-------|----------|---------|----------|
| ci-tooling | Mondays | workflow and action `uses`, CI requirement files, CI tool pins | none |
| dev-python | Mondays | `pyproject.toml` extras and the CI pins they share | none |
| sandbox-base | Mondays | base image runtime pins, agent CLIs, Node, editor extensions, Ubuntu | none |
| website | Mondays | documentation site requirements | none |
| sidecars | Mondays | sidecar image parents | none |
| riscv | 1st of the month | RISC-V tooling image pins | none |
| EDA tools | 1st of the month | one PR per tool (Yosys, Verilator, ...) | none |
| rust | 1st of the month | bwave crates and the Rust builder image | none |
| openroad, bwave-vendor, picorv32-demo, ci-python, ci-rust-nightly | any time | manual rows | Dependency Dashboard checkbox |
| major updates | the group's schedule | any group | Dependency Dashboard checkbox |
| Cargo lock-file maintenance | 1st of the month | the `Cargo.lock` files under `crates/bwave/` | Dependency Dashboard checkbox |

Tick the Dashboard checkbox to let a held update open its PR. Nothing
automerges: every PR lands through the Mergify queue.

Security updates (`vulnerabilityAlerts`) open at the next run with no
schedule, Dashboard hold, release-age wait, or hourly PR limit, and they have
their own concurrent-PR budget. They carry the `dependencies` and `urgent`
labels; `urgent` puts them in Mergify's high-priority tier. Renovate reads
Dependabot **alerts**, so keep alerts on and keep the Renovate App's
"Dependabot alerts: read" permission. Keep Dependabot security-fix PRs off
while Renovate runs, so each fix arrives once; this reports
`"enabled":false` when they are off:

```sh
gh api 'repos/{owner}/{repo}/automated-security-fixes'
```

What a security update cannot do:

- A fix for a Python range in `pyproject.toml` that already admits the fixed
  version gets no PR: security updates use `rangeStrategy` `update-lockfile`,
  and `pyproject.toml` has no lock file. Shipped images install the `==` pins
  in the requirement files, and those do get PRs.
- SHA-pinned GitHub Actions raise no alerts; their fixes arrive through the
  weekly ci-tooling group.
- Renovate applies the security settings with `force`, over every matching
  package rule. A security fix that crosses a major, hits a manual row, or
  touches a held package therefore opens without the Dashboard hold.

## Review and land a Renovate PR

1. Read the PR body: its `prBodyNotes` list places that no automatic rewrite
   can reach. Update them in the PR.
2. Check that the selected CI jobs ran and passed. The CI-only groups
   (ci-tooling, dev-python, website, and the rust crates) need nothing more.
3. For an image group (sandbox-base, EDA tools, riscv, openroad, sidecars, or
   the Rust builder image), PR CI builds a test copy of the image and runs the
   `bwave-smoke`, `sidecar-smoke`, and `riscv-image` jobs that
   `.github/scripts/ci_changes.py` selects for the changed paths. Confirm
   they ran. When no smoke job ran for a change that affects what a session
   runs, build the image locally and run a smoke session. An update must
   keep every behavior the
   [Sandbox Image contract](../SANDBOX-IMAGE-CONTRACT.md) requires.
4. Never dispatch `docker-base-publish.yml`, `docker-publish.yml`, or
   `riscv-tooling-publish.yml` on a Renovate branch. They write to the shared
   registry: a `docker-base-publish.yml` dispatch from any branch repoints the
   shared `booley-sandbox-base:main` tag.
5. Queue the PR per [merge-queue.md](merge-queue.md). The `queued` label is
   Renovate's `stopUpdatingLabel`, so Renovate leaves the branch alone while
   it is queued. Mergify validates through its own draft PRs and does not push
   to the PR branch; a dequeued PR gets Renovate updates again.

## Push to a Renovate branch

Renovate treats a branch with a commit from another author as modified and
makes no further commits to it: no rebase on conflict and no newer version.
To get a fresh Renovate update, tick the PR's rebase checkbox; this discards
the other commits. Keep the PR open: Renovate reads a closed, unmerged PR as
"ignore this update" and does not reopen it for that version (for a major
update, that major).

## Regenerate OpenROAD by hand

Renovate never bumps OpenROAD automatically: its builder images are
digest-only, and the digests Renovate proposes follow `:latest`, not the
release. An OpenROAD PR therefore carries `ready-for-human`. Its
`prBodyNotes` repeat this checklist:

- Refresh `OPENROAD_SOURCE_REF`, `OPENROAD_BINARY_VERSION`, and
  `OPENROAD_SOURCE_SENTINEL_SHA256` in `src/booley/data/docker/Dockerfile.base`.
- Regenerate `src/booley/data/docker/openroad-source-manifest.json` (the
  submodule manifest).
- Update `THIRD_PARTY_NOTICES.md`.
- Update the OpenROAD row of `docs/user/SUPPORTED-EDA-TOOLS.md`; its commit
  and self-reported version cannot be derived from the release label. Then
  regenerate the packaged copy with `python -m booley.dev_support.reference_docs`.
- Update the source-archive path in `.github/contracts/session-runtime.toml`.
- Verify that the builder-image digests in the `LABEL` lines of
  `Dockerfile.base` still match its `FROM` lines.

## Add a new pin

1. Annotate the Dockerfile `ARG` in the format the docstring of
   `tests/docker/test_dockerfile_pins.py` specifies: a
   `# renovate: datasource=... depName=...` line, or
   `# pin-companion: <PRIMARY_ARG>` for a value that moves with another pin.
   A pin Renovate must not bump (OpenROAD only) goes in `_MANUAL_PINS` and
   needs a manual-row rule with a Dashboard hold in `renovate.json`. A build
   input that names no upstream dependency goes in `_NOT_PINS`.
2. For a download with checksums or paired values, add it to `PINS` in
   `.github/scripts/pin_checksums.py`, and add every file it writes to
   `postUpgradeTasks.fileFilters` in `renovate.json`. A new post-upgrade
   command also needs a pattern in `RENOVATE_ALLOWED_COMMANDS` in
   `.github/workflows/renovate.yml`.
   `test_post_upgrade_commands_are_allowlisted_default_branch_scripts` checks
   both.
3. Give each literal copy of the pin elsewhere a custom manager, or a
   `prBodyNotes` entry when no rewrite can reach it. Write each pin once per
   manifest: Renovate tracks one copy of a name per file, so extras that need
   the same pin compose by self-reference (`dev = ["booley-rtl[test,quality]"]`).
   `test_manifests_write_each_pin_once` enforces this.
4. Assign a group rule in `packageRules`, and keep its exact-name selectors in
   step with `tests/ci/test_renovate_config.py`.
5. Validate with the strict check: the `renovate-config` CI job, or
   `renovate-config-validator --strict --no-global .github/renovate.json` from
   the Renovate runner image.

## Hold a version

Hold a version when a newer release is known bad. Record the hold in two
places that move together:

1. In the manifest, put a comment directly above the `==` pin that contains
   `hold <version>` and the reason, for example
   `# ...breaks SIGINT exit codes; hold 7.17.0 until upstream is fixed.`
2. In `packageRules`, add a rule with one `matchPackageNames` entry,
   `allowedVersions: "==<version>"`, and a `description` starting `Hold:` that
   gives the same reason and says how to lift it.

Renovate reads only the rule; the comment tells the next reader why the pin
stays. `test_version_holds_agree_with_renovate_rules` fails when one exists
without the other or the versions differ. To lift a hold, delete the comment
and the rule in one PR, then bump the pin. A security fix can still propose a
held package (see [How updates arrive](#how-updates-arrive)): check such a PR
against the reason for the hold.

## Stop Renovate

Set top-level `"enabled": false` in `.github/renovate.json` and land it through
the queue. While Renovate is stopped, turn Dependabot security-fix PRs on so
security fixes keep arriving:

```sh
gh api -X PUT 'repos/{owner}/{repo}/automated-security-fixes'
```

Turn them off again (`gh api -X DELETE` on the same path) before re-enabling
Renovate.
