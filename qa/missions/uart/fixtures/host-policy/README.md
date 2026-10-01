# Host policy inputs

Recipes for the uart mission's `host-policy` area.

Use only a disposable native QA user and a dedicated Docker daemon: host policy
applies across Projects on that daemon. Never replace the Human Maintainer's real policy.
For each case copy the named TOML bytes to a new case directory's
`xdg/booley/config.toml`. Launch the released `booley session up` from the declared
disposable Project with `XDG_CONFIG_HOME` pointing to that case's `xdg` directory.
For `config-defaults`, leave the file absent. Capture its absence before and after.

`idle.toml` sets a 30-second idle limit; save the Sandbox identity and actual expiry.
`cap.toml` sets one Sandbox; open a second declared Project on the same isolated
daemon and verify the global cap. `egress.toml` permits only the additional named
host. Supply a controlled endpoint with that exact name in the run's DNS fixture;
use a second unlisted name at the same endpoint for the deny check. The reserved
example hostname is a fixture identity, not an Internet service.

For each `invalid-*.toml`, save a copy of the file, the Docker inventory and the Project
manifest before invoking the public bootstrap command. Require the named policy
rejection, identical file bytes, and no bootstrap mutation. Copy `recovery.toml`
over the case-owned file, repeat the same command and require fresh success.
A parser-only check cannot establish Sandbox, session-cap, or egress behavior.

List the case directories and sessions in `resources.md`. Stop the run-owned
sessions and reconcile the Docker inventory before deleting case directories.
Keep the failed attempt and restoration artifacts separately under `evidence/`.

For migration cases, use `legacy.toml` and `both-tables.toml` in separate case
users. From an initialized disposable Project, run the released
`booley init --check-only` and `booley doctor --concise` on the host with the
case's `XDG_CONFIG_HOME`. Capture the actual command output and exit codes, and
compare the host config bytes before and after both commands.

- `legacy.toml`: both commands must show the deprecation warning and the exact
  `[sandbox]` replacement preserving 600 seconds, two Sandboxes and the named
  egress host. Replace the case-owned legacy table with the printed replacement;
  repeat both commands and require the host-table warning to disappear.
- `both-tables.toml`: both commands must say `[interactive]` is ignored. The
  effective cap is one, timeout is the default 7200 seconds, and there is no
  additional egress hostname; legacy fields must not be merged. Verify the cap
  and denied extra egress with the same runtime controls described above.
- Put `max_sessions = 2` under Project `booley.toml [sandbox]` while retaining
  its valid image/memory settings. Init must reject it before Project mutation;
  Doctor must name the host config destination. Restore the Project fixture and
  repeat successfully. Save the unchanged Project bytes and inventory evidence.

Parser and adapter tests validate these authored fixtures, but do not replace
the real CLI evidence required for this Mission.
