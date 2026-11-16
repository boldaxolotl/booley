# Reviewer

The **Reviewer** is a [Specialist](SPECIALISTS.md) that reads your RTL or
testbench code and reports the problems it finds, graded CRITICAL, MAJOR, or
MINOR. It looks for problems that deterministic EDA tools can't judge because
they depend on the design's intent: RTL that doesn't match its spec, protocol
and clock-domain-crossing (CDC) mistakes, security weaknesses, and tests that
pass without checking anything. Each review covers one focus, so it looks hard
at one kind of problem instead of skimming for everything at once. It reads
code and never edits it. For a pass/fail verdict from an EDA tool, use a
[Flow](FLOW_REFERENCE.md) instead.

## Choosing a focus

| Code | Focus | What it checks |
|---|---|---|
| RTL | `spec` | The RTL does what the spec asks, no more and no less |
| RTL | `bugs` | Bug patterns, synthesis hazards, reset/width/signing mistakes, `ifdef`/config consistency |
| RTL | `protocol` | Bus/protocol rules, handshakes, ordering, and clock-domain crossings (CDC) |
| RTL | `security` | Fault-injection resistance, simple power/timing leakage, secret exposure |
| RTL | `optimization` | Dead RTL and power/performance/area wins with no trade-off |
| RTL | `code_style` | Comments, naming, readability, magic values, assertion quality |
| Testbench | `quality` | False passes, missing checks and edge cases, coverage gaps, sampling mistakes |

The `bugs` review also flags some problems that [Lint](FLOW-LINT.md) and
synthesis already report, such as latches and width mismatches.

A `bugs`, `protocol`, or `security` review runs two searches side by side: one
checks the code against a list of known patterns, the other hunts by tracing
scenarios. The `bugs` hunt traces scenarios such as reset, back-to-back
operations, and full or empty states; the `protocol` hunt traces stalls,
back-pressure, aborts, and clock or reset domain crossings through each
interface; the `security` hunt traces attacker-controlled inputs, debug and test
access, resets, and error and fault paths. An `optimization` review pairs its
checklist with a hunt for redundant, oversized, or critical-path logic and
registers that could share storage. Each reports the findings of both searches
and merges findings the two searches report at the same file and line. It uses
about twice the model time and tokens of one search. A search that fails runs
once more; if it fails again, the whole review is an error instead of a partial
result.

A testbench `quality` review runs two searches the same way: the checklist for
the testbench language (SystemVerilog, cocotb or C/C++), and a hunt that traces
how each test decides pass or fail, looking for paths where a test passes
without checking the DUT. The hunt follows the pass and fail messages and
trace files your project configures. Expect about twice the model time and
tokens of the checklist alone.

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

A new review replaces the findings of the previous one.

### Reviewing only a change

Ask for a review of what changed since a commit or branch, for example
*"Review my changes since `main` for bugs."* The Reviewer then reports only on
the changed files, using the unchanged ones as context and the diff and commit
messages as its brief. Problems that were already there before the change are
listed separately as pre-existing, and never count as open. A change that
touches none of the files in scope finishes without a review.

### What a review can read

Each Reviewer has the other side's files hidden, as your `.core` filesets
declare them, and any HDL file (`.v`, `.vh`, `.sv`, `.svh`, backups like
`rom.sv.orig` included) that no fileset declares, unless the review's scope
names that file. A scope that names a file your filesets declare for the
other side is refused. Every other project file, firmware included, is readable
context. A finding in a file outside the review's files is kept in the review
evidence and never counts as open.

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
```

| Option | Effect |
|---|---|
| `--category rtl\|tb` | Review RTL or testbench code. |
| `--focus <focus>` | One focus from [the table above](#choosing-a-focus). |
| `--scope <file,...>` | Files to review: Verilog and SystemVerilog (`.v`, `.vh`, `.sv`, `.svh`); with `--category tb` also Python (`.py`) and C/C++ sources and headers. |
| `--spec <path>` / `--spec-text "<text>"` | The specification to check against (one, not both). Required for `--focus spec`. |
| `--steer "<text>"` | Extra context for the Reviewer. Repeatable. |
| `--base <commit>` | [Review only the change](#reviewing-only-a-change) since the merge-base of `<commit>` and HEAD, uncommitted changes included. Pass every file of the category as `--scope` so the Reviewer also reads the files that use the changed ones. |
| `--fail-on critical\|major\|minor` | Exit 1 when an open finding is at or above that severity. Without it, a completed review exits 0 even with open findings. |
| `--dry-run` | Preview the call without running the review: lists the checks it would run; a `bugs`, `protocol`, `security`, `optimization` or testbench `quality` review shows two. |

The options every Specialist shares are in [Specialists](SPECIALISTS.md#running-a-specialist).
