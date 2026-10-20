# Doctor health and deep validation

Doctor diagnoses Booley's build and execution machinery in a Project's environment,
including its integration with Project build configuration. Normal Booley Flows
verify the design. A design failure that the machinery executes and interprets
correctly is compatible with healthy machinery. Doctor's existing verdict rules
still apply; deep currency reporting does not change their classification.

A Claude Code or Codex session on the host is expected during Project Setup. `session.agent-on-host`
is a PASS-level reminder that the Booley MCP server and Booley Flows live only
in the Sandbox. For Interactive work, reopen in the devcontainer or use
`booley session up && booley session enter`. This reminder needs no waiver.

Use plain `booley doctor` for ongoing work. It checks current setup and reports
one informational line about prior deep validation. Run `booley doctor --deep`
inside the Sandbox during initial Project Setup, after a Booley version change,
after replacing the active Sandbox Image, or when investigating suspected
machinery problems.

Doctor warns with `git.attributes-policy-unknown` when a registered sibling
worktree's user attributes cannot be inspected or Git cannot report its system
attributes path. An unknown policy is an inspection limitation, not a confirmed
conflict. Existing attributes and worktree registrations are preserved, and
Project Initialization defers new shared local defaults until policy inspection
is complete. Inspect an unavailable sibling from an environment where its
registered path is accessible (the host if needed), or inspect the selected
attributes file's type and access. For unsupported system-path discovery, use
a Git build that supports discovery to inspect the system policy; the supported
minimum Git version is unchanged.

This warning can persist on supported older Git or in a Sandbox without host
sibling access. Plain Doctor still exits 0 when there are only warnings, but
unclean health prevents recording clean Doctor evidence. Normal init remains
nonfatal; unknown-only `booley init --check` exits 1 and reports incomplete
inspection. A waiver of `git.attributes-policy-unknown` does not waive genuine
attributes conflicts or current-checkout inspection errors. Existing
`git.line-endings-unreadable` waivers are not automatically applied to this new
warning. Resolve the inspection limitation or accept it through a targeted
waiver; do not remove a valid local rule or prune registrations to hide it.

Deep Doctor is **current** when a qualifying deep run matches the executing
Booley version and active immutable Sandbox Image, and no later saved completed failed,
warning-blocked, or incomplete deep attempt supersedes it. The recorded date is
informational;
deep evidence has no age expiry. Current means prior qualifying setup validation;
it does not guarantee a new deep run will pass after arbitrary design edits.

Host-side status reporting reads the package version in the observed Sandbox;
a host-only upgrade does not replace that execution version. Inside the Sandbox,
Doctor uses its live package version. An unverifiable version makes deep due.

The two automatic change triggers are:

- A change to the executing Booley version.
- A change to the active immutable Sandbox Image (Docker local image ID).

Project configuration, waiver, `.core`, RTL, testbench, and self-test fixture
edits do not make deep Doctor due. Plain Doctor and affected normal Flow runs
exercise those changes. Moving a mutable image tag does not change the identity
of a Sandbox already running from that image. Selecting an image in configuration
also does not substitute it for the active image.

A qualifying deep run must have no active health failures or unwaived warnings
except exactly `upgrade.review-pending`, the release-review gate. It must complete
every applicable deep check: the Specialist provider authorization
and memory probe, selected Simulation/Lint/ASIC Synthesis smokes, enabled sim/lint
good-and-bad self-tests, and selected-core live resolution. Disabled capabilities
and intentionally excluded FPGA implementation are legitimate exclusions.
Missing required configuration, unavailable execution, absent probe evidence,
omitted agent checks, and missing self-test execution prevent qualification.
Waiving a warning does not execute the missing check.

ASIC Synthesis smoke evidence is mode-aware for every selected synthesis Target,
including existing Projects and port setups. Logical mode requires completed
synthesis (`termination: completed`), `ppa_complete: true`, and Yosys mapped-area
evidence (`area_source: yosys_mapped`); it makes no constrained-timing claim.
Physical mode still requires complete PPA and constrained-clock timing with
nonempty per-clock results. Reports with absent or empty mode follow
that physical timing policy; an unknown mode is invalid evidence. This proves
the Flow wiring, not that the design meets its PPA goals.

Deep Doctor observes the Sandbox Image identity and Booley version once, when the
deep phase starts. A host Booley version that differs from the Sandbox's package
version prevents qualification. Core resolution runs in the same Sandbox as the Flow checks. Local
FuseSoC resolution inside that Sandbox does not require Docker on its PATH.
A host-side deep invocation cannot qualify when it skips the in-Sandbox Specialist
provider probe; run deep Doctor inside the Sandbox for full evidence.

A saved completed failed, warning-blocked, or incomplete deep attempt retains historical
success but makes deep Doctor due until a later qualifying run succeeds. Warning-only
attempts say “did not qualify” and name the unwaived warning IDs; actual failures
say “failed.” Older unsuccessful records with unknown health details say “did not
qualify; legacy health verdict details unavailable.” Cancellation or an
unexpected abort before a completed result leaves the previous evidence intact;
version or image drift still applies independently. Successful and failed plain
runs leave deep evidence unchanged.

Every issued Sandbox spec, headless or VS Code, carries the pinned image ID in its
`BOOLEY_SANDBOX_IMAGE_ID` container environment variable, covered by the spec
digest. Inside the Sandbox, Doctor reads the image identity from that variable
and the version from the installed Booley package. On the host, Doctor selects the
running Sandbox and reads both from that container. A missing or malformed
variable reports `cannot verify Sandbox Image identity`, and an absent, ambiguous,
or unreadable Sandbox reports identity and version unavailable; deep Doctor stays
due. The variable is issued environment metadata, not tamper-proof evidence: a
process can override its own environment. A Sandbox whose environment lacks the
variable does not match the current host issuance; one ordinary refresh, using
the remedy Booley prints, fixes it.

Missing, corrupt, unsupported, and plain-only stamps report no qualifying deep
run recorded. Plain stamps with `deep: true` lack image and completeness
proof and cannot establish currency. A qualifying new run establishes evidence.
Deep records live at `<booley_dir>/runtime/doctor_deep_stamp.json`, independently
of the existing plain stamp's configuration fingerprint and age policy.

Plain Doctor and `booley session up` report **current** with version/date or
**due** with all applicable reasons. Manual and automatic structured reports at
`<booley_dir>/runtime/doctor/last.json` include `deep_status` with the same status,
reason codes and messages, and historical success.
If saving fails, Doctor logs one informational line: “deep result was not saved”.
Status is computed from the saved record, whose bytes remain intact;
no new saved success is claimed.

Deep due is advisory: it does not affect health warnings, exit codes or execution, and never schedules or automatically launches deep
checks. Automatic plain Doctor retains its existing scheduling policy.

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

Acknowledgment compares the expected target with the running version and the
locked persisted pending target. A changed target, stale runtime, corrupt or
missing state, or missing packaged release entry rejects a pending transition
without erasing it. An exact retry with no pending target and reviewed version
equal to both the running and expected versions exits 0 with “already current”
and leaves state untouched. The same result on an initially current baseline
does not prove a new release review occurred.

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
  `default_seed` on an unseeded Target. See
  [Tests (`tests.toml`)](CONFIG.md#tests-teststoml).
- `tickets.leftover-board`: nonempty `tickets/board`, `tickets/state`,
  `tickets/logs`, `tickets/locks`, or `tickets/waiver-candidates` is reported
  by directory name. Preserve needed work, then delete that directory. Empty
  directories are listed in the same line when applicable but do not warn on
  their own. `tickets/history/` is silent.
- `tickets.leftover-guidance`: `ticket_creation.md` or `ticket_defaults.md`
  matches a shipped template, so delete it; if edited or unfamiliar, move its
  rules into a Goalset under `.booley/goalsets/`, then delete it.

Both IDs are stable and waivable through `doctor-waivers.toml`. Doctor also
WARNs on ignored `[developer]` settings and `[jobs].max_tickets` with a delete
fix. These settings do not configure Goal Mode.

Doctor WARNs with `workspace.legacy-source-stash` when an interrupted run left
a source stash record for this worktree; see
[manual source recovery](TROUBLESHOOTING.md#recover-sources-hidden-by-an-interrupted-older-run).
