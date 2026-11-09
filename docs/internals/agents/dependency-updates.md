# Dependency updates

Renovate is the only update bot. Its config is `.github/renovate.json`; the
runner is `.github/workflows/renovate.yml` (every 4 hours, plus
`workflow_dispatch`). Dependabot version updates were removed in #2562.
`tests/ci/test_renovate_config.py` pins the contract below.

## How updates arrive

| Group | Schedule | Touches | Approval |
|-------|----------|---------|----------|
| ci-tooling | weekly | workflow and action `uses`, CI requirement files, CI tool pins | none |
| dev-python | weekly | `pyproject.toml` extras and the CI pins they share | none |
| sandbox-base | weekly | base image runtime pins, agent CLIs, Node, editor extensions, Ubuntu | none |
| website | weekly | documentation site requirements | none |
| sidecars | weekly | sidecar image parents | none |
| riscv | monthly | RISC-V tooling image pins | none |
| EDA tools | monthly | one PR per tool (Yosys, Verilator, ...) | none |
| rust | monthly | bwave crates and the Rust builder image | none |
| openroad, bwave-vendor, picorv32-demo, ci-python, ci-rust-nightly | any time | manual rows | Dependency Dashboard checkbox |
| major updates, Cargo lock-file maintenance | per group, monthly for maintenance | any | Dependency Dashboard checkbox |

Tick the Dashboard checkbox to let a held update open its PR. Nothing
automerges: land every PR through the Mergify queue (`merge-queue.md`).

Security updates (`vulnerabilityAlerts`) open at the next run with no
schedule, no Dashboard hold and no release-age wait. They carry
`dependencies` and `urgent` on purpose: `urgent` is Mergify's high-priority
tier. Renovate reads Dependabot **alerts**, so keep alerts on and keep the
Renovate App's "Dependabot alerts: read" permission. Dependabot security-fix
PRs are off; do not re-enable them while Renovate runs.

Limits:

- A fix for a Python range in `pyproject.toml` that already admits the fixed
  version gets no PR (`pyproject.toml` has no lock file, and vulnerability
  fixes use `rangeStrategy` `update-lockfile`).
  Shipped images install the `==` pins in the requirement files, and those do
  get PRs.
- SHA-pinned GitHub Actions raise no alerts; their fixes arrive through the
  weekly ci-tooling group.
- Security settings are forced over every matching rule (Renovate's
  `force` merge), so a security fix that crosses a major or hits a manual row
  opens without the Dashboard hold. Renovate's hourly PR limit skips security
  fixes; the concurrent PR limit still counts them, in a separate counter.

## Review and land a Renovate PR

1. Read the PR body: `prBodyNotes` list places no rewrite can reach.
2. Check the CI jobs ran and passed. CI-only groups (ci-tooling, dev-python,
   website, rust crates) need nothing more.
3. Image groups (sandbox-base, EDA tools, riscv, openroad, sidecars, the Rust
   builder image): PR CI builds a test copy of the image and runs the
   `sidecar-smoke` and `bwave-smoke` jobs that
   `.github/scripts/ci_changes.py` selects. Confirm they ran. When a smoke job
   was not selected for a change that affects what a session runs, build the
   image locally and run a smoke session.
4. Never dispatch `docker-base-publish.yml`, `docker-publish.yml` or
   `riscv-tooling-publish.yml` on a Renovate branch: a dispatch publishes and
   moves shared tags.
5. Queue it per `merge-queue.md`. The `queued` label is Renovate's
   `stopUpdatingLabel`, so Renovate stops touching the branch while queued.
   Mergify validates through its own draft PRs and does not push to the PR
   branch; a dequeued PR resumes normal updates.

## OpenROAD manual regeneration checklist

An OpenROAD PR is a human job (`ready-for-human`). Same items as its
`prBodyNotes`:

- Refresh `OPENROAD_SOURCE_REF`, `OPENROAD_BINARY_VERSION` and
  `OPENROAD_SOURCE_SENTINEL_SHA256` in `src/booley/data/docker/Dockerfile.base`.
- Regenerate `src/booley/data/docker/openroad-source-manifest.json` (the
  submodule manifest).
- Update `THIRD_PARTY_NOTICES.md`.
- Update the OpenROAD row of `docs/user/SUPPORTED-EDA-TOOLS.md`.
- Update the source-archive path in `.github/contracts/session-runtime.toml`.
- Verify the builder-image digests in the `LABEL` lines of `Dockerfile.base`
  match its `FROM` lines.

## Add a new pin

1. Annotate the Dockerfile `ARG` as `tests/docker/test_dockerfile_pins.py`
   specifies: `# renovate: datasource=... depName=...`; a pin that follows a
   branch uses `packageName=` and `currentValue=<branch>` over a commit digest;
   a tag-plus-commit pair adds `# pin-companion: <PRIMARY_ARG>`. A pin that
   must not be bumped automatically (OpenROAD only) goes in `_MANUAL_PINS`
   and needs a manual-row rule with a hold in `renovate.json`; a build input that
   is not a pin goes in `_NOT_PINS`.
2. A download with checksums or paired values: add it to `PINS` in
   `.github/scripts/pin_checksums.py`. Keep `fileFilters` in `renovate.json`,
   the allowlist in `.github/workflows/renovate.yml` and
   `tests/ci/test_renovate_config.py` in step.
3. A literal copy of the pin elsewhere needs a custom manager, or a
   `prBodyNotes` entry when no rewrite can reach it.
4. Assign a group rule in `packageRules`, and keep its exact-name selectors in
   step with `tests/ci/test_renovate_config.py`.
5. Validate with the strict check (`renovate-config` CI job, or
   `renovate-config-validator --strict --no-global .github/renovate.json` from
   the runner image).

## Pushing to a Renovate branch

Renovate treats a branch with a commit from another author as modified and
makes no further commits: no rebase on conflict, no newer version. For a fresh
Renovate update, tick the PR's rebase checkbox; it discards the follow-up
commits. Do not close the PR: Renovate reads a closed, unmerged PR as "ignore
this update" and will not reopen it for that version (for a major, that major).

## Rollback

Revert the cutover in a PR: `git revert -m 1 <cutover merge commit>`. That
restores `.github/dependabot.yml`, the global hold and the tests that assert
them together, so the required `renovate-config` job stays green. Hand edits
alone fail `test_cutover_lifts_the_global_hold_and_drops_dependabot`.

To stop Renovate entirely, also set top-level `"enabled": false` in
`.github/renovate.json`. Re-enable Dependabot security fixes with
`gh api -X PUT repos/boldaxolotl/booley/automated-security-fixes`.
