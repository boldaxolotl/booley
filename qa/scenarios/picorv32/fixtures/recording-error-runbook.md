# PicoRV32 stimulus and verdict preflight

This pack is versioned with the Scenario. Run the named read-only validator on
disposable, run-owned inputs **before** invoking its Check. Retain the validator
JSON or failure text, exact command, source identities, and independent observed
report under that Check's `evidence/` capture directory. A validator success is
only a valid stimulus, never product Qualification. Do not copy host paths,
credentials, licenses, or mutable host registry state into this pack.

An invalid static setup found in a separate preparatory Step can be recorded as
an Observation and repaired before a Check attempt. Once a Check command is
attempted, append its `blocked` Check Result with the invalid input and log
immediately, then retain any corrected attempt. If no valid attempt occurs, the
final Check status remains `blocked`. A valid stimulus that exposes a product
failure is `fail`; do not relabel it as setup error. Record the declared expected
result and independent observed result even when they disagree.

## Fixture commands and exact oracles

Use `python3 qa/scenarios/picorv32/fixture_validation.py` in these examples.
The Project path and refs come from this run's ledger, not from a copied run.

* `git-topology ROOT inventory`: both outer and `.booley_project` Git markers
  must resolve to their own checkouts before bounded Project discovery. Scan
  only the declared ROOT; a directory symlink under it must not become a root.
* `git-topology ROOT synth --outer-ref REF --inner-ref REF`: the nested Project
  must be a **linked** worktree (`.git` file with usable gitdir), both refs must
  resolve, and the declared baseline and candidate must produce structured
  synthesis reports with numeric delta and both identities. The standalone
  nested topology used for Ticket Create is deliberately different.
* `git-topology ROOT ticket --outer-ref REF --inner-ref REF`: the nested Project
  must have a standalone `.git` directory and both paired destination refs
  must resolve before invoking the Ticket Create skill. Retain an exact ref and
  topology snapshot. Recreate the intended topology between synth and Ticket
  Create; do not repair it by changing a borrowed Project.
* Build `fixtures/riscv/spike-probe.S` with `spike-probe.ld` using the selected
  runtime's RISC-V compiler. Run `spike-elf ELF --ram-start START --ram-end END`
  with the RAM bounds documented for that runtime before invoking Spike. Every
  PT_LOAD range must fit, including ELF-header padding; a successful firmware
  compile alone does not prove valid Spike execution.
* Use `fixtures/lint/qa_lint_fixture.sv` in two distinct lint Targets. It
  produces `WIDTHTRUNC` on supported Verilator settings. Compare both
  per-Target structured `lint_report.json` files and the aggregate using
  `lint_dedupe`: the same rule/file/line/message must occur in both, and the
  aggregate must contain it once with both Target results. An unused local
  variable is not a proven warning stimulus.
* For native waivers, use the checked-in Verilator `.vlt` (including the
  `` `verilator_config`` directive) and Verible `.txt`. Render the Verible RTL
  with `python3 fixtures/lint/render_verible.py qa_lint_fixture.sv` in a
  disposable Target source directory: it contains exact trailing-space bytes
  and a module name matching its filename. Capture before/after reports for
  each linter and use `lint_waiver`; only the intended warning may disappear.
  If a separate control warning exists, pass its rule as `control_rule` and
  require it to remain. A clean report without a proved before warning cannot
  demonstrate waiver behavior.
* Before `lint-hard`, run `lint-command booley session enter -- booley ... lint
  ...` with the exact argv that will be executed. Exit 125 from a malformed
  `session enter` command is a Scenario Operator command error, not a lint verdict.
* For `grant-recovery`, use `vivado-mount MOUNT REGISTERED_SOURCE` after
  registry and runtime metadata identify the canonical source and mount.
  The validator resolves `bin/vivado` or `Vivado/bin/vivado` from that source;
  never assume a host release layout. Capture the actual version invocation.

For retained structured reports, pass `oracle INPUT.json`. The input has a
`kind` plus the named data: `lint-dedupe` takes `first`, `second`, `combined`
lint reports; `lint-waiver` takes `before`, `after`, `intended_rule` and optional
`control_rule`; `synth-baseline` takes the retained `summary`, full `report`,
and `expected` candidate/baseline Target names, Target identities, and full
`candidate_revision` and `baseline_revision` commit IDs plus the declared
`delta_pct` and `timing_delta_pct`;
`vivado-implementation` takes `report`, `artifacts`, `before`, and
`retained_dir`; `stealth-native` takes `paths`; `bwave-mode` takes `child` and
`replay`; `verdict` takes `declared_expected` and `observed`. The verdict
oracle prints both values and exits nonzero on disagreement. Retain its JSON
beside the unmodified product report.

The remaining verdicts use these explicit boundaries:

* `riscv-tools`: verify GCC, srec_cat, dtc, and Spike executable/version
  outputs, then run `pdf-subjects --subject SUBJECT ... PDF ...` against the
  offline PDFs for the declared ISA, privileged, debug, and other required
  subjects. Several subjects may share one PDF. A count of four PDF files is
  not the oracle.
* Vivado registration: run `vivado-registration REQUESTED REGISTERED` to
  compare resolved canonical sources. A symlink spelling difference is normal.
* Vivado implementation: require a fresh uncached PASS and the declared routed
  checkpoint, timing report, and utilization report tied to that invocation.
  The Check does not declare bitstream generation; absence of `.bit` is not a
  failure. Missing or stale declared artifacts is a failure.
* `fpga-authority-fail`: **pass** when the own revoked Grant produces the
  declared authority denial (exit 2), with no unauthorized FPGA execution.
  Grade that negative result before any later regrant/recovery.
* B-Wave distance: parse documented `@ start -> @ end d=...` rows, verify
  `end-start == d`, and compare the full ordered relation. The validator's
  `distance LOG 6,7,6` form checks an expected sequence. For a child diagnosis cross-check,
  replay its exact trace, signal paths, sampling, clock/reset options and
  defaults first. Explicit sampling flags are not equivalent to a child's
  default mode unless proved on the same waveform.
* Stealth projection native-file inventory: exclude any path component
  beginning with `.` from native counts; retain the raw inventory separately.

## Grant and Session transition

Use the run-owned exact Project root and registration IDs, saving host `--json`
and Session command outputs before and after each transition. The read-only
`state_probe.py` accepts a compact JSON snapshot with `owner`, `states`, and
`protected`. `owner` has `project_root`, `registration`,
`run_owned_registrations`, and the retained `evidence_root`.
Each state contains that `project_root`, `eda_kind: vivado`,
`grant_registration`, integer `grant_epoch`, `session_grant_epoch`, Boolean
`session_valid`, `session_running`, and `mount_probe`. Use `null` for a missing
Grant/issuance. Epochs are evidence labels for the exact Grant transition and
issued spec; never guess them from wall-clock time. The `states` keys are
`before`, `revoked`, `regranted`, `issued`, and `started`; for downstream Checks
also include `current`. `protected.before` and `protected.after` each hold the
complete borrowed `grants` and `installations` lists; the probe requires them
to remain identical. `denial_evidence` holds `declared_expected: denied`,
`flow_executed: false`, `log_path`, and `log_sha256` from the negative Check
alone. The retained log must identify the FPGA command, record exit 2, and
contain the authority denial.

The replacement probe requires the old and intended registrations in the
run-owned ledger, increasing Grant epochs for revoke then regrant, an unchanged
old Session epoch with invalid/stopped/unmounted state until issuance, and an
issued Session epoch equal to the new Grant epoch before start and mount.

1. Inspect the existing Grant and Session. If a different Grant is attached,
   revoke it only when the ledger proves it belongs to this run. Never modify
   a borrowed Grant or Installation Registration. If the intended Grant is
   already attached, keep it and run the `ready` probe rather than replacing it.
2. For the negative Check, revoke only the run-owned Grant, capture its denial,
   and run `python3 qa/scenarios/picorv32/state_probe.py SNAPSHOT denied`.
3. Add the intended run-owned Grant. A regrant alone leaves the prior Session
   spec stale. Run `booley init --seed` on the host, then `booley session
   validate` and `booley session up`; use `booley session enter --` with a
   concrete command to prove the mounted executable. Run `state_probe.py
   SNAPSHOT replace` and then `ready` on the latest `current` snapshot.
4. Before Doctor, FPGA, or any dependent Check starts, run the `ready` probe.
   Its failure names the missing transition. Preserve the before/revoked/
   regranted/issued/started snapshots and ledger IDs through cleanup. Release
   only run-owned Grants, registrations, and Sessions; prove borrowed identities
   unchanged.

## Recording-error traceability

The frozen Scenario at `77cb964a` and updated `main` had byte-identical
`scenario.yaml` before these edits. The table maps all 20 recording-error cases
from the sealed run to the affected Check or record boundary. Record-integrity
cases belong to measure 3 and are intentionally outside this change.

| Case | Check / boundary | Failure mechanism | Revised oracle or gate |
| --- | --- | --- | --- |
| `0c23af03cc98bb8d` | `riscv-tools` | Four-PDF count assumption | Required subjects in PDF content |
| `11ec87e465af1995` | `bwave.semantic-distance` | Parsed wrong text format | `@ start -> @ end d=` and numeric relation |
| `274b675d353a090e` | `lint-native-waiver` | Invalid `.vlt`, extra Verible warning | Native waiver and control-warning comparison |
| `2a2f557642706bfb` | Check Result recorder | Correction ordinal repair | Measure 3; unchanged here |
| `3fae982e35868542` | Ticket Create payload | Nested Git file at Create | Standalone nested Git and paired refs |
| `483332649cb8f9c9` | `synth-baseline` | Unsupported nested topology | Linked nested worktree and numeric report |
| `50308ff003c5e44f` | `vivado.project-grant` | Added over existing Grant | Inspect/revoke own old Grant first |
| `758184282e05a330` | `lint-hard` | Omitted executable after `enter` | Exact argv preflight |
| `83cd8e52beef9ffc` | Stealth projection | Counted dot-prefixed projection | Exclude hidden path components |
| `8fbf0ecc7056dbeb` | `riscv-firmware` | ELF segment below RAM | PT_LOAD bounds check |
| `943a310fab8666b7` | `inventory-import` | Outer `.git` absent | Both real Git markers before scan |
| `95e2c8b6bf2928b4` | Ticket Create payload | Paired Project unavailable | Exact standalone topology and refs |
| `b3f348c9621e85b4` | `fpga-authority-fail` | Denial misgraded, stale spec | Independent denial; explicit reissue |
| `b57f1b0913b6f072` | `vivado.implementation` | Added `.bit` requirement | Fresh routed DCP/timing/utilization |
| `da46e6d3f35b07cd` | Check Result evidence link | Disposable external path | Measure 3; unchanged here |
| `de4d2d77bc166679` | `grant-recovery` | Wrong mount executable path | Resolve from registered release layout |
| `e94ac2372fad0775` | `vivado.registration` | Lexical symlink comparison | Canonical resolved source equality |
| `ece0c0b1b2588aab` | `interactive.bwave-diagnosis` | Wrong replay sampling | Match child's exact query/defaults |
| `f0da87052d0acc48` | `lint-dedupe` | No real duplicate warning | Same structured warning in both Targets |
| `fd6fb54952e08743` | `doctor-recovery` | Stale Session after regrant | Ready-state probe before Doctor |
