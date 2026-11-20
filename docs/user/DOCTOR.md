# Doctor

`booley doctor` checks that Booley's setup works in your Project: the
configuration, Targets and `.core` files, the Sandbox and its image, the
EDA tools, and the agent's access to its model provider. Run it after setup,
after updating Booley, and whenever a Flow or the agent stops working.

Doctor checks the machinery, not your design. A simulation that runs and
reports a failing test is healthy machinery; the Flows judge the design.

## Run Doctor

Doctor checks what it can see from where it runs, so it gives a different view
from each side:

- **Host terminal:** the Booley install, Docker, the Sandbox Image, and the
  Project configuration. Doctor starts the Sandbox for its Flow dry-runs. It
  never completes pending Sandbox recovery: it warns and skips the checks that
  need the Sandbox until `booley session up` completes it.
- **Sandbox terminal** (a VS Code terminal in the devcontainer, or
  `booley session enter`): the same configuration plus the tools, MCP server,
  and agent setup inside the Sandbox.

Run plain `booley doctor` for everyday checks. It checks configuration and
prepares each Flow without running a simulation, lint, or synthesis.

Run `booley doctor --deep` in the Sandbox when Doctor says deep validation is
due (see [Deep checks](#deep-checks)). It runs real short simulations, lints,
and syntheses and, when the Project uses the FPGA Flow, an FPGA implementation.
It can take minutes or tens of minutes, and longer with an FPGA implementation.

| Option | Effect |
|---|---|
| `--concise` | Hide passed checks |
| `--verbose`, `-v` | Show extra detail, including waivers that matched no warning |
| `--deep` | Also run the real EDA checks described under [Deep checks](#deep-checks) |
| `--skip-agent-checks` | With `--deep`, skip the agent credential check and the model-provider probes |
| `-C PATH`, `--project PATH` | Check the Project at `PATH` instead of the one containing the current directory |

## Read the result

Each line starts with a glyph:

| Glyph | Meaning |
|---|---|
| `✓` | Passed |
| `!` | Warning: something is wrong or at risk; fix it or [waive it](#doctor-waivers-doctor-waiverstoml) |
| `✗` | Failure: that part of Booley won't work until it is fixed |
| `~` | Waived warning, shown with the reason from your waiver |
| `·` | Note or skipped check; read it, usually no action needed |

A problem line is followed by a `→` line with the suggested fix. Warnings
also print an ID such as `sim.trace-unavailable:sim_fast`; you need it only to
write a waiver. The **Summary** at the end lists every open failure and warning.

The exit code is 1 when anything failed and 0 otherwise, so a run with only
warnings still exits 0. Setup is clean only when there are no `✗` and no `!`
lines.

## Fix problems

Type `/booley-heal` in a Claude Code or Codex chat. The agent reads the Doctor
output, fixes what it can inside the Sandbox, and re-runs Doctor until it is
clean. Steps only you can take, such as installing something on the host or
renewing a license, it hands to you with exact instructions. It asks before it
waives a warning, and it doesn't change your RTL or testbench unless you ask it
to.

To fix a problem yourself, follow the `→` hint under it.

### Goal Mode leftovers

| Check ID | Level | Meaning and fix |
|---|---|---|
| `config.retired-goals` | `✗` | `booley.toml` has a `[goals]` section. Delete its tables, or ask the Developer Agent to run `/booley-heal`. |
| `goals.leftover` | `!` | A `goals/` or `goalsets/` directory is still there. Booley does not read or delete it; delete it once you do not need its records. |

### Stealth Mode core copies

| Check ID | Level | Meaning and fix |
|---|---|---|
| `stealth.projections-not-current` | `!` | A root copy of a core in `.booley/cores/` is missing or out of date. Any Flow run refreshes it; so does `booley init`, where its hint says to run it. |
| none | `✗` | A foreign file sits where Booley writes a copy: move or delete it. Or a core in `.booley/cores/` cannot be read or lacks its leading `CAPI=2:` line: fix the core, then run any Flow. Or the copies are not excluded from Git status: run `booley init` on the host. |

While a copy is missing, out of date or blocked, Doctor skips the Flow
dry-runs, the `.core` audit and the deep checks.

## Automatic checks

Booley runs plain Doctor in the background when the Sandbox starts, if the
last result is older than 7 days (1 day if it found problems) or older than a
change to the Project's configuration or the Booley version.

It never blocks work. New problems show up in the output of
`booley session up`, at the top of the next Flow result the agent receives,
and on the Dashboard. The full output of the last run is in
`.booley/runtime/doctor/last.log`.

## Deep checks

`booley doctor --deep` adds checks that run real tools:

- one short, paid call on your default Agent CLI and one on each model a
  Specialist runs on, which proves the credentials work in this Sandbox. A
  model the provider refuses fails Doctor and names the `[models]` entry to
  change, instead of failing a run later. `--skip-agent-checks` skips these
  calls;
- a short simulation, lint, and ASIC synthesis, and an FPGA implementation when
  the Project uses the FPGA Flow, on each Target marked for Doctor (see
  [Marking Targets for Doctor](TARGETS.md#marking-targets-for-doctor)). An FPGA
  implementation runs for as long as the [FPGA Flow](FLOW-FPGA.md)'s time limit
  allows;
- the simulation and lint [self-tests](#self-test-fixtures).

### When deep checks are due

Plain Doctor and `booley session up` each print one line about deep checks:

- **current**: the last deep run qualified, on this Booley version and this
  Sandbox Image. It doesn't expire with time, and editing configuration, RTL,
  or tests doesn't change it; plain Doctor and your Flow runs cover those.
- **due**: the last deep run didn't qualify, or Booley was updated or the
  Sandbox Image replaced since. The line says why.

A deep run qualifies when it runs in the Sandbox without `--skip-agent-checks`,
every check that applies actually ran, and it reports no failures and no
warnings except `upgrade.review-pending`. A waived warning doesn't count as a
check that ran.

Doctor never starts deep checks on its own, and a due status doesn't count as a
warning.

## Self-test fixtures

A check that never fails proves nothing. With self-test fixtures,
`booley doctor --deep` runs simulation and lint on a known-bad input as well as
a good one, and fails if the bad input passes. Without them, Doctor warns
`flow.fail-path-unvalidated`.

Ask the agent to add them; Project Setup normally does. They put deliberately
broken files under `.booley/selftest/` and in a lint Target that
`booley targets` hides. Only deep Doctor uses them.

Without them, deep checks can't become current, even if you waive the warning.

## Upgrade review

After a Booley update, Doctor warns `upgrade.review-pending` until someone
checks what the new release changes for this Project. Type `/booley-heal` in a
Claude Code or Codex chat. The agent reads the release notes since your last
reviewed version, applies the changes the Project needs, verifies them with
Doctor, and marks the review done. It asks you to run any step it can't reach.

## Doctor waivers (`doctor-waivers.toml`)

Some warnings describe a deliberate choice, such as a testbench that can't
write traces. Record that choice in `.booley/doctor-waivers.toml`. Waived
warnings still show, marked `~` with your reason, but don't count as
problems. Failures can't be waived.

Copy the `check` and `subject` from the warning's ID: in
`sim.trace-unavailable:sim_fast`, the check is `sim.trace-unavailable` and the
subject is `sim_fast`.

```toml
version = 1

[[waiver]]
check = "sim.trace-unavailable"
subject = "sim_fast"
reason = "The upstream C++ harness has no trace switch; functional simulation is sufficient."
expires = 2026-12-01

[[waiver]]
check = "project.git-excludes-missing"
subject = ".devcontainer"
reason = "This project deliberately tracks .devcontainer in its repository."
permanent = true
```

| Key | Rule |
|---|---|
| `version` | Must be the integer `1` |
| `check` | Required; the part of the ID before the colon |
| `subject` | Optional; must match exactly. Leave it out to waive the check for every subject |
| `reason` | Required; shown next to the waived warning |
| `expires` | A date (`YYYY-MM-DD`); the waiver applies through that day |
| `permanent` | `true` for a waiver that never expires |

Each waiver needs exactly one of `expires` or `permanent = true`. Prefer
`expires` for temporary limits. An expired waiver stops matching and Doctor
notes it. `booley doctor --verbose` notes waivers that matched no warning, so
you can delete them.

If the file has a typo, an unknown key, or two waivers for the same check and
subject, Doctor fails instead of guessing what you meant.

If Doctor gets something wrong, report it with the `booley-feedback` skill; see
[Troubleshooting](TROUBLESHOOTING.md).
