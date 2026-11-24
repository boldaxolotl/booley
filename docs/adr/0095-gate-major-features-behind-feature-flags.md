# Gate major features behind Feature Flags

Status: accepted (2026-11-24)

Decided in [#3829](https://github.com/boldaxolotl/booley/issues/3829).

## Context

Every merge to `main` ships in the next release, and the weekly release is
automated. No rule said what a patch release may contain, so a large feature
or a breaking change could reach users in a patch.

## Decision

- **Automated weekly releases are patch releases.** A minor release is the
  maintainer's decision.
- **A patch needs no manual upgrade action**, removes or renames no
  user-facing surface, and changes no default.
- **A major feature merges behind a Feature Flag.** A breaking change is gated
  with the old behavior as the default, or merges only while a minor release
  is being prepared.
- **A Feature Flag is registered in code with a name, a one-line summary, and
  a tracking issue**, and is enabled per Project by
  `[features].enable` in `booley.toml`. Flags are maintainer-only: no
  environment variable, no user setting, no user documentation.
- **Gated surfaces are absent, not disabled.** While its flag is off, a
  command or an MCP tool is missing from help and from the tool catalog and
  behaves as unknown when used. Generated references list every surface with
  all flags off.
- **A flag is enabled by deleting it, and only in a minor release.** The PR
  that deletes the flag publishes the feature's user documentation.
- **A name in `[features].enable` that is not registered fails configuration
  loading**, so a stale entry never hides a deleted flag.

## Considered options

- A user-facing opt-in: it would need user docs and a support promise for
  unfinished features.
- An environment variable: it bypasses the Project's own record and reaches
  agents in a Sandbox.
- Long-lived feature branches: they drift from `main` and defer integration
  problems to the merge.

## Consequences

- A gate adds one registry entry and one query, and the deleting PR removes
  them, so an unfinished feature costs `main` almost nothing.
- Help and docs from a flag-off Project cannot show a flagged feature, so a
  maintainer tests one from inside a Project that enables it.
