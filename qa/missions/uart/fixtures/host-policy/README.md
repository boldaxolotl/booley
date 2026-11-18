# Host policy inputs

Recipes for the uart mission's `host-policy` area: how to apply each TOML
fixture in this directory and what to observe. Parser and adapter tests
validate these fixtures, but only real CLI evidence counts for the mission.

## Isolation

Use only a disposable native QA user and a dedicated Docker daemon: host policy
applies to every Project on that daemon. Never replace the Human Maintainer's
real policy. For each case, copy the named TOML bytes to a new case directory's
`xdg/booley/config.toml`, then run the installed `booley session up` from the
declared disposable Project with `XDG_CONFIG_HOME` pointing at that case's
`xdg` directory. For the defaults case, leave the file absent and capture its
absence before and after.

List the case directories and sessions in `resources.md`. Stop the run-owned
sessions and reconcile the Docker inventory before deleting case directories.
Keep failed attempts and restoration artifacts separately under `evidence/`.

## Single-field cases

- `idle.toml` sets a 30-second idle limit; save the Sandbox identity and its
  actual expiry.
- `cap.toml` allows one Sandbox; open a second declared Project on the same
  isolated daemon and verify the global cap.
- `egress.toml` permits one additional named host. Serve a controlled endpoint
  under that exact name in the run's DNS fixture, and use a second, unlisted
  name for the same endpoint for the deny check. The reserved example hostname
  is a fixture identity, not an Internet service.

A parser-only check cannot establish Sandbox, session-cap or egress behavior.

## Invalid files and recovery

For each `invalid-*.toml`, save a copy of the file, the Docker inventory and
the Project manifest, then run the public Bootstrap command. Require the named
policy rejection, identical file bytes and no Bootstrap mutation. Copy
`recovery.toml` over the case-owned file, repeat the same command and require
fresh success.

## Migration cases

Run `legacy.toml` and `both-tables.toml` as separate case users. From an
initialized disposable Project, run `booley init --check-only` and
`booley doctor --concise` on the host with the case's `XDG_CONFIG_HOME`.
Capture the actual output and exit codes, and compare the host config bytes
before and after both commands.

- `legacy.toml`: both commands show the deprecation warning and the exact
  `[sandbox]` replacement, preserving 600 seconds, two Sandboxes and the named
  egress host. Replace the case-owned legacy table with the printed
  replacement; repeat both commands and require the host-table warning to
  disappear.
- `both-tables.toml`: both commands say `[interactive]` is ignored. The
  effective cap is one, the timeout is the default 7200 seconds, and there is
  no additional egress hostname: legacy fields are not merged. Verify the cap
  and the denied extra egress with the same runtime checks as the single-field
  cases.
- Put `max_sessions = 2` under the Project's `booley.toml [sandbox]`, keeping
  its valid image and memory settings. Init must reject it before changing the
  Project; Doctor must name the host config as the destination. Restore the
  Project fixture and repeat successfully. Save the unchanged Project bytes and
  inventory evidence.
