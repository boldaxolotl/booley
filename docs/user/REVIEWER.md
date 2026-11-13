# Reviewer

The **Reviewer** is a [Specialist](SPECIALISTS.md) that reads your RTL or
testbench code and reports the problems it finds, graded CRITICAL, MAJOR, or
MINOR. Each review covers one focus, such as bugs or protocol compliance, so
it looks hard at one kind of problem instead of skimming for everything at
once. Use it for a second opinion on a change before you simulate or merge it.
It reads code and never edits it. For a pass/fail verdict from an EDA tool,
use a [Flow](FLOW_REFERENCE.md) instead.

## Choosing a focus

| Code | Focus | What it checks | When |
|---|---|---|---|
| RTL | `spec` | The RTL does what the spec asks, no more and no less | Before simulation |
| RTL | `bugs` | Bug patterns, synthesis hazards, reset/width/signing mistakes, `ifdef`/config consistency | Before simulation |
| RTL | `protocol` | Bus/protocol rules, handshakes, ordering, and clock-domain crossings (CDC) | Before simulation |
| RTL | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure | After simulation passes |
| RTL | `optimization` | Dead RTL and power/performance/area wins with no trade-off | After simulation passes |
| RTL | `code_style` | Comments, naming, readability, magic values, assertion quality | After simulation passes |
| Testbench | `quality` | False passes, missing checks and edge cases, coverage gaps, sampling mistakes | Any time |

The early reviews are an engineer's quick look at freshly written code: they
catch bugs before they cost time in simulate-and-fix loops.

A review sees only one side of the RTL/testbench split. A testbench review
never reads the RTL, so it judges the tests against the spec rather than
against what the RTL happens to do.
[Features](FEATURES.md#independent-specialists) explains why this matters.

## Running a review

1. Ask the agent, naming the focus and the files:

    > *"Review the FIFO RTL for bugs."*
    >
    > *"Check `rtl/axi_bridge.sv` against `docs/spec.md`."*

    A `spec` review needs the specification: point the agent at the file, or
    paste the text.

2. The agent replies with the findings and the path to the report. Each
   finding has an ID, a severity, and a location.
3. Decide which findings to fix, and ask the agent to fix them.
4. Ask the agent to verify the fixes. A verify rechecks only the open findings
   of the latest review and looks for nothing new. To argue a finding away,
   give the agent the reason; the Reviewer accepts or rejects it. A review
   allows two verifies; after that, run a fresh review.

A new review replaces the findings of the previous one.

### Reviewing only a change

Ask for a review of what changed since a commit or branch, for example
*"Review my changes since `main` for bugs."* The Reviewer then reports only on
the changed files, using the unchanged ones as context and the diff and commit
messages as its brief. Problems that were already there before the change are
listed separately as pre-existing, and never count as open. A change that
touches none of the files in scope finishes without a review.

### Firmware

Outside Goal Mode, the Reviewer also reads your firmware as context: the
non-HDL files under the project's `fw/` directory. HDL files under `fw/`
(backups like `rom.sv.orig` included) still count as RTL. The Reviewer never
reports findings in firmware; such a finding is shown as out of scope and
never counts as open. A review of firmware files alone is refused.

## In Goal Mode

A [Review Goal](GOAL-MODE.md#review-goals) such as `REVIEW:rtl_bugs` fixes the
files, the spec, and the verdict, so you don't pass them. A `done` Goal only
needs the review to run; a `clean` Goal needs every finding fixed or waived,
and the agent drives the fix-and-verify loop above on its own. Reviews you run
by hand never count toward a Goal.

## Your project's style rules

The `code_style` and testbench `quality` reviews follow style guides shipped
with Booley. Add your own rules in `.booley/rtl_style_guide.md` (RTL) and
`.booley/tb_style_guide.md` (testbench), in plain Markdown. Where your rules
and Booley's conflict, yours win.

## Running it yourself

In a container terminal:

```bash
booley specialist reviewer --category rtl --focus bugs --scope rtl/fifo.sv
booley specialist reviewer --category rtl --focus spec --scope rtl/fifo.sv --spec docs/spec.md
booley specialist reviewer --verify
```

| Option | Effect |
|---|---|
| `--category rtl\|tb` | Review RTL or testbench code. |
| `--focus <focus>` | One focus from [the table above](#choosing-a-focus). |
| `--scope <file,...>` | Files to review. |
| `--spec <path>` / `--spec-text "<text>"` | The specification to check against (one, not both). Required for `--focus spec`. |
| `--steer "<text>"` | Extra context for the Reviewer. Repeatable. |
| `--base <commit>` | [Review only the change](#reviewing-only-a-change) since the merge-base of `<commit>` and HEAD, uncommitted changes included. |
| `--fail-on critical\|major\|minor` | Exit 1 when an open finding is at or above that severity. Without it, a completed review exits 0 even with open findings. |
| `--verify` | Recheck the latest review's open findings. Accepts only `--waive` and `--fail-on`. |
| `--waive <finding-id> "<reason>"` | With `--verify`: propose a waiver. Repeatable. |
| `--dry-run` | Preview the call without running the review. |

The options every Specialist shares are in [Specialists](SPECIALISTS.md#running-a-specialist).
[CLI-REFERENCE.md](CLI-REFERENCE.md#reviewer) has the full reference,
including which source file types each category accepts.
