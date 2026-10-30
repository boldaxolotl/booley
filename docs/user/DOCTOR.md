# Doctor health and deep validation

Doctor checks that Booley's setup and tools work in this Project. It doesn't
verify your design; Flows do that.

A Claude Code or Codex session on the host is expected during Project Setup. `session.agent-on-host`
is a PASS-level reminder that the Booley MCP server and Booley Flows live only
in the Sandbox. For Interactive work, reopen in the devcontainer or use
`booley session up && booley session enter`. This reminder needs no waiver.

Use plain `booley doctor` for ongoing work. It checks current setup and reports
one informational line about prior deep validation. Run `booley doctor --deep`
inside the Sandbox during initial Project Setup, after a Booley version change,
after replacing the active Sandbox Image, or when investigating suspected
machinery problems.

Plain Doctor checks FPGA Targets' setup. Deep mode prints their manual
full-implementation commands instead of launching long Vivado runs.

## Automatic checks

Booley also runs plain Doctor on its own when the container starts and on
supported command and Flow paths: about once a week, daily while problems
remain, and whenever the configuration changes. It never blocks work. New
problems show up in `booley session up` and the next Flow result. The last
result is in `.booley/runtime/doctor/last.log`.

## Unknown Git attributes policy

Doctor warns with `git.attributes-policy-unknown` when it can't inspect a
registered sibling worktree's attributes, or Git can't report its system
attributes path. This is an inspection limit, not a confirmed conflict.
Inspect the sibling from an environment that can reach it (the host if needed),
or accept the limit with a targeted waiver. That waiver doesn't cover real
attributes conflicts. Don't delete a valid local rule or prune worktree
registrations to hide the warning.

## Deep Doctor currency

Deep Doctor is **current** when the latest completed deep attempt qualified and
matches the running Booley version and the active Sandbox Image. Deep evidence
has no age expiry. Current means setup was validated; it doesn't promise a new
deep run will pass after design edits.

Deep becomes **due** when either of these changes:

- the running Booley version;
- the active Sandbox Image.

Configuration, waiver, `.core`, RTL, testbench, and self-test fixture edits
don't make deep due. Plain Doctor and normal Flow runs exercise those changes.

A deep run qualifies only if it has no failures and no unwaived warnings other
than `upgrade.review-pending`, and every applicable deep check ran: the
Specialist provider probe, the selected Simulation, Lint and ASIC Synthesis
smokes, the enabled sim/lint self-tests, and live core resolution. Disabled
capabilities and FPGA implementation are excluded. ASIC smoke must complete with
full PPA. Waiving a warning does not run the missing check.

Run deep Doctor inside the Sandbox. A host-side deep run that skips the
in-Sandbox Specialist provider probe can't qualify.

A failed attempt says “latest deep attempt failed” and names up to eight
blocking check IDs (a blocker with no ID shows as `unidentified-failure` or
`unidentified-warning`). An incomplete one says “applicable deep checks
incomplete” and names the checks that didn't run. Deep stays due until a later
attempt qualifies. A cancelled run leaves the previous result in place.

If Doctor says it cannot verify Sandbox Image identity, run
`booley session refresh`.

Deep due is advisory: it doesn't change health warnings or exit codes, and
Doctor never launches deep checks by itself.

## Self-test fixtures

`doctor --deep` checks that sim and lint fail on a known-bad input as well as
pass on a good one.

- **Lint:** add a Target named `lint_selftest_bad` in its own tracked `.core`,
  with `flow_options.booley.doctor_selftest: true`. Keep its known-bad source
  beside that core in the tracked tree. Doctor uses the first lint Doctor
  Target as the good case and this Target as the bad case. `booley targets`
  and ordinary Flow selection hide Doctor self-test Targets.
- **Simulation:** mirror the replacement file beneath
  `.booley/selftest/sim/bad-overlay/` (for example,
  `bad-overlay/firmware/firmware.hex`). Deep Doctor overlays that tree on an
  isolated build of the default Target's first runnable test. Ordinary
  simulation never uses it. Don't put Doctor-only commands in
  `[flows.sim].pre_run_commands`.

## Upgrade review

A Booley version change records a pending release review and emits
`upgrade.review-pending`. This gate remains an active warning for plain Doctor
health, but alone does not prevent a complete deep run from qualifying. Every
other active warning, every failure, and missing execution evidence still blocks
deep qualification.

Run `booley upgrade status --json` to capture the pending target. Review state is
`runtime/upgrade_review.json` inside the resolved Booley Directory (default
`.booley/`, configurable per Project). Invoke `/booley-heal` to review the packaged
changes and repair findings. Verify plain Doctor in the Sandbox, then one
qualifying deep Doctor in the Sandbox, and plain Doctor on the host before:

```console
booley upgrade acknowledge --expected-target <captured-target>
```

If the target changed since you captured it, acknowledge refuses; capture it
again. Running it twice is harmless.

Run final plain Doctor to confirm the pending warning disappears and deep remains
current; acknowledgment and plain Doctor preserve the qualifying deep evidence.

## Project checks and Goal state

The **Project checks** section validates Project Git state, configuration,
custom endpoints, and Goal presence. Goal warnings distinguish active records
from quiet attributable session presence; quiet does not prove abandonment.

- `tests.toml`: one line says whether the file loads (section keys, Test and
  Test Variant names, `select` and `seed` templates). A failure names the
  Target and the key. For each sim Target, one more line says how its Test
  Runs are seeded and with which Default Seed, for example `Target 'sim' Test
  Runs are seeded through Verilator +verilator+seed+<seed> (Default Seed 1)`,
  or that they are unseeded and why. An unseeded Target is reported as a
  fact, not as a problem. A `tests.toml` entry that the Target cannot apply
  fails: a Test Variant argument that overrides a plusarg the Target
  declares, arguments or a `seed` template on a Cocotb Target, or
  `default_seed` on an unseeded Target. A section key that names no Target,
  or two keys that name one Target, also fail and name the key or keys. See
  [Tests (`tests.toml`)](TESTS.md).

`handles.map-size` (subject `session` or `job`) warns when a Dashboard handle
map (`runtime/sessions/handles/map.json` or `runtime/job-handles/map.json`)
is larger than 12 MiB, three quarters of its 16 MiB cap. Past the cap, new
sessions and Jobs cannot get handles. Run `booley session down`, then
`booley session up`: the Booley MCP server prunes expired handles when it
starts. It is waivable through `doctor-waivers.toml`.

Doctor WARNs with `workspace.legacy-source-stash` when an interrupted run left
a source stash record for this worktree; see
[manual source recovery](TROUBLESHOOTING.md#recover-sources-hidden-by-an-interrupted-older-run).

## Doctor waivers (`doctor-waivers.toml`)

`booley doctor` reserves `WARN` for an actionable failure mode. A clean setup
has no active warnings: fix the cause, or explicitly accept a deliberate
project constraint in `.booley/doctor-waivers.toml`. Waived findings
remain visible and are counted separately; the file does not hide
output or change failures.

Each warning prints a stable identity such as `sim.trace-unavailable:sim_fast`.
The part before the colon is `check`; the part after it is the optional
`subject`. Match that identity exactly:

```toml
version = 1

[[waiver]]
check = "sim.trace-unavailable"
subject = "sim_fast"
reason = "The upstream C++ harness has no trace switch; functional simulation is sufficient."
expires = 2026-11-01

[[waiver]]
check = "project.git-excludes-missing"
subject = ".booley"
reason = "This project deliberately tracks .booley in its parent repository."
permanent = true
```

The schema is intentionally strict:

- `version` must be the integer `1`.
- Every `[[waiver]]` needs a lowercase dot/dash-separated `check` and a
  non-empty `reason`.
- `subject`, when present, is an exact string match. Without it, the entry
  matches every subject for that check; use that broad scope only when one
  justification genuinely applies to all findings.
- Set exactly one of `expires` (a TOML date, active through that date) or
  `permanent = true`. Prefer expiration for temporary environment or upstream
  constraints.
- A duplicate `check`/`subject` pair, unknown key, invalid type, or malformed
  TOML makes Doctor fail rather than silently ignoring the intended policy.

Expired entries do not match; Doctor emits a note and reports the warning
again. `booley doctor --verbose` also notes active entries that matched no
current warning, which is the cue to delete stale waivers. Waivers apply only
to warnings—never failures or notes—and match structured IDs rather than
human-readable message text.
