# H-12 host-policy inputs

Use only a disposable native QA user and a dedicated Docker daemon: host policy
applies across Projects on that daemon. Never replace the operator's real policy.
For each case copy the named TOML bytes to a new case directory's
`xdg/booley/config.toml`. Launch the released `booley session up` from the declared
disposable Project with `XDG_CONFIG_HOME` pointing to that case's `xdg` directory.
For `config-defaults`, leave the file absent. Capture its absence before and after.

`idle.toml` sets a 30-second idle limit; retain runtime identity and actual expiry.
`cap.toml` sets one session; open a second declared Project on the same isolated
daemon and verify the global cap. `egress.toml` permits only the additional named
host. Supply a controlled endpoint with that exact name in the run's DNS fixture;
use a second unlisted name at the same endpoint for the deny check. The reserved
example hostname is a fixture identity, not an Internet service.

For each `invalid-*.toml`, record file SHA-256, Docker inventory and Project
manifest before invoking the public bootstrap command. Require the named policy
rejection, identical file bytes, and no bootstrap mutation. Copy `recovery.toml`
over the case-owned file, repeat the same command and require fresh success.
A parser-only check cannot establish runtime, session-cap or egress behavior.

Stop the run-owned sessions and reconcile the Docker inventory before deleting
case directories. Keep the failed attempt and restoration artifacts separately.
