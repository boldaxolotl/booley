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
  Project configuration.
- **Sandbox terminal** (a VS Code terminal in the devcontainer, or
  `booley session enter`): the same configuration plus the tools, MCP server,
  and agent setup inside the Sandbox.

Run plain `booley doctor` for everyday checks. It checks configuration and
prepares each Flow without running an EDA tool.

Run `booley doctor --deep` in the Sandbox when Doctor says deep validation is
due (see [Deep checks](#deep-checks)). It runs real short simulations, lints,
and syntheses, and can take minutes or tens of minutes.

| Option | Effect |
|---|---|
| `--concise` | Show only problems and the final counts |
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

To fix a specific message yourself, look it up in
[Troubleshooting](TROUBLESHOOTING.md).

## Automatic checks

Booley runs plain Doctor in the background when the Sandbox starts, if the
last result is out of date:

- a clean result older than 7 days;
- a result with problems older than 1 day;
- or a change since the last run to `booley.toml`, `tests.toml`,
  `configs.toml`, `doctor-waivers.toml`, `devcontainer.json`, `AGENTS.md`,
  `CLAUDE.md`, a `.core` file, or the Booley version.

It never blocks work. New problems show up in the output of
`booley session up`, at the top of the next Flow result the agent receives,
and on the Dashboard. The full output of the last run is in
`.booley/runtime/doctor/last.log`.

## Deep checks

`booley doctor --deep` adds checks that run real tools:

- one short, paid call on your default Agent CLI, which proves the credentials
  work in this Sandbox and measures the agent's memory use, plus one more short
  call for each model a Specialist runs on, made through that model's Agent CLI
  (a model Codex reaches through a gateway makes no extra call). A model the
  provider refuses fails Doctor and names the `[models]` entry to change,
  instead of failing a Goal later (skip these calls with
  `--skip-agent-checks`);
- a short simulation, lint, and ASIC synthesis on each Target marked for Doctor
  (see [Doctor selection](CONFIG.md#explicit-flow-targets-and-doctor-selection));
- the simulation and lint [self-tests](#self-test-fixtures);
- FuseSoC resolution of every core.

FPGA implementation is too slow for these checks. For each FPGA Target, deep
Doctor prints the command to run it yourself, for example
`booley flow fpga --target fpga_arty`.

### When deep checks are due

Plain Doctor and `booley session up` each print one line about deep checks:

- **current**: the last deep run passed on this Booley version and this
  Sandbox Image. It doesn't expire with time.
- **due**: there is no passing deep run since Booley was updated or the
  Sandbox Image was replaced. Editing configuration, RTL, or tests doesn't make
  deep checks due; plain Doctor and your Flow runs cover those changes.

Doctor never starts deep checks on its own, and a due status doesn't count as a
warning.

A deep run makes deep checks current only if it has no failures, no warnings
except `upgrade.review-pending`, and every check that applies actually ran.
Waiving a warning doesn't count as running the check. These runs never
qualify:

- a run on the host, because the model-provider probes run only in the
  Sandbox;
- a run with `--skip-agent-checks`.

When the last attempt didn't qualify, the due line says why:

- `latest deep attempt failed (...)` lists up to eight IDs of the checks that
  blocked it.
- `applicable deep checks incomplete (...)` lists the checks that didn't run.
- `cannot verify Sandbox Image identity`: on the host, start the Sandbox with
  `booley session up`. If the message stays, run `booley session refresh` to
  replace the Sandbox.

A cancelled deep run leaves the previous status unchanged.

## Self-test fixtures

A check that never fails proves nothing. With self-test fixtures,
`booley doctor --deep` runs simulation and lint on a known-bad input as well as
a good one, and fails if the bad input passes. Without them, Doctor warns
`flow.fail-path-unvalidated`.

Ask the agent to add them; Project Setup normally does. Each fixture puts a
deliberately broken file in your repository:

- **Simulation:** a replacement file under `.booley/selftest/sim/bad-overlay/`,
  at the same relative path as the file it replaces, for example
  `.booley/selftest/sim/bad-overlay/firmware/firmware.hex`. Deep Doctor applies
  it only to its own build of the first test of the first Target marked for
  Doctor simulation. Ordinary simulation never uses it.
- **Lint:** a Target named `lint_selftest_bad` in its own committed `.core` file, with
  `flow_options: {booley: {doctor_selftest: true}}`, next to a small source
  file that should fail lint. `booley targets` and normal Target selection
  hide it.

If you don't want broken files in the repository, waive the warning instead.

## Upgrade review

After a Booley update, Doctor warns `upgrade.review-pending` until someone
checks what the new release changes for this Project. Type `/booley-heal` in a
Claude Code or Codex chat. The agent reads the release notes since your last
reviewed version, applies the changes the Project needs, and verifies with
plain and deep Doctor in the Sandbox and plain Doctor on the host (it asks you
to run any step it can't reach). Once everything is clean, it marks the review
done. `booley upgrade status` shows where the review stands.

If Doctor instead warns `upgrade.runtime-stale`, the Sandbox still runs an
older Booley. Run `booley session refresh` on the host first.

## Unknown Git attributes policy

`git.attributes-policy-unknown` means Doctor couldn't read the Git attributes
of another worktree of this repository, or Git couldn't report where its
system attributes file is. It isn't a confirmed conflict. Check that worktree
from somewhere that can reach it, such as the host, or waive the warning.
Don't delete working attribute rules or prune worktrees just to silence it.

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
subject = ".booley"
reason = "This project deliberately tracks .booley in its parent repository."
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
