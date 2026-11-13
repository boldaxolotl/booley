# Mutation fixture recipes

This runbook gives the QA agent running the [mutation
mission](../../missions/mutation/MISSION.md) the fixture layout, staging, and
fault files. The mission says what to exercise; this file says exactly how.
`<asset>` below is `qa/shared/mutation/` in the Booley checkout under test.
Goal-child rules are not needed: no area here runs a Goal.

## Fixture

`<asset>/project/` is a small SystemVerilog design with two simulation Targets.
Native builds run only in the Sandbox.

| Path | Role |
|---|---|
| `rtl/mutation_top.sv` | Top level, instances only: no sites |
| `rtl/alu.sv` | `alu` and `alu_shift` (two modules in one file) |
| `rtl/counter.sv` | Counter holding the equivalent Mutant (`q <= q;`) |
| `rtl/status.sv` | Logic no test checks: every Mutant survives |
| `rtl/handshake.sv` | `assign ack = req;`: its Mutant hangs both testbenches |
| `rtl/self_check.sv` | Constant function checked by an elaboration `$error`: its Mutants do not build |
| `rtl/decoys.sv` | Text and constructs that must never be mutated, plus two bit-select and three deletion candidates |
| `rtl/vendor/sync2.sv`, `rtl/gen/version.sv` | Tagged `vendored` and `generated`: outside the default scope |
| `tb/mutation_tb.sv`, `tb/test_mutation.py` | Icarus and cocotb testbenches (`full`, `smoke`, `fail`): outside the default scope |
| `mutation.core` | Targets `sim_icarus` (Icarus) and `sim_cocotb` (Verilator, `-Werror-USERERROR`) |
| `project-data/tests.toml` | Registers `full` and `smoke` on both Targets; `fail` is not registered |

`full` sweeps `alu`, `alu_shift`, the counter, and the decoy outputs
exhaustively against a reference in the testbench. `smoke` only drives inputs.
Both wait for `ack` first and have no watchdog, so a Mutant that never drives
`ack` runs until Booley's time limit.

## Staging

1. Copy `<asset>/project/` as a fresh disposable Project (add it to
   `resources.md`), initialize it through the documented public route, and
   install `project-data/tests.toml` into the Booley Directory the Project
   resolves (never assume `.booley`).
2. Commit the fixture and the initialized Project files.
3. Baselines: `booley flow sim --target sim_icarus` and
   `booley flow sim --target sim_cocotb` each pass. Run through
   `booley session enter -- ...` from the host.

Fixture bytes and the registry are fixed. Never edit them to make a run pass:
a mismatch against `expected.json` is the finding.

## Reading `expected.json`

Written by hand from the fixture and `docs/user/FLOW-MUTATION.md`, never from
generator output. Mutant IDs are hashes and cannot be derived by hand, so the
file names a Mutant as `file:line:site->replacement`; read these from the
run's `mutation-specs.md`.

- `operators`: every candidate Mutant per operator; an exhaustive run
  (`--count` equal to the total) must produce exactly this set.
- `decoys`: text a Mutant must never change (`never`: the forbidden
  replacement, or `*` for any change).
- `outcomes`, `per_file`, `totals`: expected survivors, equivalent, hanging and
  invalid Mutants, and the exhaustive tally and verdict.
- `auto`, `selection`: `--count auto` sizes and the round-robin operator order.
- `rule` in a candidate quotes the user doc sentence that makes it a
  candidate. Only the entries marked `code` come from the code.

### Site granularity

The report shows these texts, so `site` uses them. They are the one thing the
doc does not state, so this table follows the code:

| Operator | `site` |
|---|---|
| `relational`, `arithmetic-logical` | the operator token |
| `condition` | the predicate without its parentheses |
| `constant` | the value digits only (`2` of `4'd2`) |
| `bit-select` | the index or `msb:lsb` inside the brackets |
| `deletion` | the whole statement including `;`, replaced by `/* deleted */` (module-level `assign`) or `begin end` (procedural) |

## Fault files

Each fault works on a run-owned copy. Save the failed output, then restore.

| File | Install | Expected | Restore |
|---|---|---|---|
| `faults/tests-baseline-fail.toml` | Over `tests.toml` in the Booley Directory | The `fail` test fails the baseline: `ERROR`, exit 2 | Copy `project-data/tests.toml` back |
| `faults/syntax_error.sv` | Over `rtl/status.sv` | The run names `rtl/status.sv` and the parser diagnostic, exit 2 | Copy the original back |
| `faults/unelaborated.sv` | Over `rtl/mutation_top.sv` | The baseline does not elaborate: `ERROR`, exit 2 | Copy the original back |

## Hand-checked simulator behavior

Checked on the fixture before the oracle's `outcomes` were written (plain
`booley flow sim`, hand-applied edits, no generator): `smoke` passes while
`full` fails on a relational edit in `alu.sv`; deleting the `ack` assignment
hangs and ends in the simulation timeout on both Targets; an edit to `bump`
fails the build on both Targets (Icarus reports `ERROR`, Verilator under
`-Werror-USERERROR` reports `FAIL`). The Mutation Flow must still call such a
Mutant `invalid` on both Targets.

On `sim_cocotb`, a Verilator Target, the Flow also compares each Mutant's
generated C++ with the baseline's and replaces an identical one with the next
candidate (see the user doc's section on equivalent Mutants). The fixture's
equivalent Mutant (`rtl/counter.sv:11`) is not one of them on Verilator 5.052,
the version this was checked with: it writes different C++ for it, so it still
runs and survives, and `equivalent_dropped` stays empty. Another Verilator
version may drop it; that is expected, not a finding. On `sim_icarus` nothing is compared.
