# Verilator 5.052 migration acceptance

Validated on 08 SEP 2026 for [issue #393](https://github.com/boldaxolotl/booley/issues/393).
The immutable release is `v5.052`, commit
`ea338be98e1e838d3518809ce8899f85a009963c`. The annotated tag object is
`efa4927be48e75c3cd08fc848b198d1d9d237f00`; do not use that object as the
post-checkout HEAD assertion.

The [upstream comparison](https://github.com/verilator/verilator/compare/a6f4dd031f50387ae0169490c6d8843b91dd1c07...v5.052)
reports the release 212 commits ahead of the required nested-shift fix, with
`a6f4dd031f50387ae0169490c6d8843b91dd1c07` as its merge base.

## Build and runtime contract

Both `Dockerfile.base` and the application `Dockerfile` were built locally.
Validation used `booley-runtime-base:issue-393` and `booley-sandbox:issue-393`,
built from the migration worktree on main commit `dc7691b7`. The application
wheel was built from that worktree. Neither image was published. The complete
standard Session Runtime image contract passed, including the base-layer
relationship, exact Verilator version, source record, and LZ4 headers.

The compiler stage and runtime install `liblz4-dev`: users compile native FST
models in the runtime, so the builder's headers alone are insufficient.
The compiler retains the system allocator; `ldd verilator_bin` confirms no
jemalloc linkage. No allocator change or performance claim accompanies this
upgrade. The runtime retains the exact source identity at
`/usr/local/share/verilator/BOOLEY-SOURCE.txt`.

## Repeatable checks

The candidate-image CI job runs
`python tests/docker/verilator_acceptance.py --work-dir <empty-directory>`.
The runner uses the standard library, fails on missing prerequisites, bounds
every compiler and simulator invocation, and retains command logs, XML results,
and raw databases. The original eight acceptance tests passed in the candidate image.
CI uploads those artifacts even on failure. The 76 Dockerfile/base-contract
tests and `ruff check src/ tests/` also passed.

| Contract | Evidence |
| --- | --- |
| Compiler correctness | Both nested variable left/right shifts exercise every pair of two-bit shift amounts against independent C++ expectations, including overflow. The fixture follows upstream #7955. |
| Force/unpacked arrays | Timed generated-main simulation checks force/release on an unpacked array element and an unpacked element's bit select. The locally available downstream force/unpacked-array design also elaborated successfully (14 modules); its private source is not part of this repository. |
| Harnesses | Untimed authored C++ and timed generated SystemVerilog; Cocotb 2.1.0 drives timed events and checks outputs. The existing native FST suite also compiles and runs an authored timed C++ tracing harness. |
| Diagnostics and verdicts | Lint and C++ elaboration pass; a delay in a final block fails even with `-Wno-fatal`. Generated simulations cover pass, `$fatal`, bounded timeout, and SIGTERM. Version 5.052 emits an unclassified `%Error` for the final-block fixture, not a `FINALDLY` code. |
| Seeds | Repeated generated-main and Cocotb runs use explicit nonzero seed 123, compare random sequences, and retain separate native outputs. |
| Native format | Strict `SystemC::Coverage-3` header and count records; line, branch, expression, both directional toggles, properties, zero-hit points, and a source filename containing spaces. |
| Instance identity | Equal-parameter and different-parameter instances remain distinct in generated-main, authored-main, and Cocotb raw output. One equal-parameter instance hits a property while the other remains zero. |
| Reset window | Authored C++ zeroes all counters at an explicit boundary and writes a database proving all values are zero before subsequent stimulus. Other harnesses include initialization/reset activity. |
| Native aggregation | Preserve two raw files; parse their full keys before merging; compare every merged counter with independent addition. Exercise both native summary and hierarchy reports. |
| Waveforms | Existing B-Wave native FST suite: authored `VerilatedFstC` harness passes, plus 28 FST/VCD command pairs with zero differences across counter, FSM, and LFSR fixtures. |
| Booley integration | Existing Cocotb counter project: Verilator and Icarus each pass reset, count, and overflow through the installed Booley flow. Related host simulation/lint/registry tests: 999 passed, 4 explicit skips, 1 deselected. |

## Harness requirement discovered during acceptance

`--coverage-per-instance` at compilation is necessary but insufficient for an
authored harness. Without writer setup, equal-parameter instances were merged
into `same_*` in the actual raw database.

Use the four explicit instrumentation switches
`--coverage-line --coverage-toggle --coverage-expr --coverage-user`, plus
`--coverage-per-instance`. Then configure the writer:

- Generated `--binary`/`--main`: Verilator inserts the writer setup.
- Authored C++: call `context.coveragep()->forcePerInstance(true)` before writing.
- Cocotb 2.1.0: also pass `--coverage-per-instance` to the simulation executable
  (the runner's `test_args`), not just the compiler.

All three writers honor `+verilator+coverage+file+<unique-path>`.
Cocotb needs no source patch. Runtime writer setup is visible in its installed
`share/lib/verilator/verilator.cpp`; the Verilator API is documented in the
[pinned runtime header](https://github.com/verilator/verilator/blob/v5.052/include/verilated_cov.h).

## Coverage feature boundary

These additional checks qualify the compiler and native collection interfaces.
The Verilator pin and native collector landed separately in PR #233 before
this acceptance work was integrated with current main. The acceptance reader
is a test-only native format probe, not a second scoring implementation.

The existing campaign schema and native collector tests cover unknown-record
retention, incompatible-format handling, and partial-selection accounting.
Those production-adapter checks remain separate from this native writer probe.
Covergroups share `--coverage-user`; an adapter must classify them as
unsupported, along with experimental FSM and unknown records, rather than
silently score them as properties. The fixture intentionally uses no covergroup
or FSM instrumentation.

Native B-Wave parsing and GTKWave 3.3.116 waveform decoding are validated
(see the review follow-up below). No coverage denominator equivalence with
5.046 is claimed. Keep tool version, full source
identity, instrumentation flags, seed, and elaboration identity when interpreting
normalized artifacts.


## Review follow-up on current main

The candidate was rebuilt with the package from current main at `84b25152`.
The required image gate now also invokes
`tests/docker/verilator_flow_acceptance.py`. It compares ordinary
`SimulationExecution` with `collect_target_coverage` across generated-main,
custom-main, and Cocotb harnesses, with five outcomes each: pass, test failure,
compile failure, timeout, and SIGTERM. These are real builds and real adapter
processes, not substituted execution results. Each simulator gets a two-second
runtime bound; the invoking shell gets a separate bounded compilation allowance.

The rebuilt candidate passed all nine acceptance tests, including all 15
verdict pairs matching the expected contract and each other (76 seconds).
The follow-up host suite passed 966 tests, with 4 explicit skips and 1
deselection; Ruff and the 104 image/workflow/helper checks passed. The
new matrix exposed and fixed a production collector precedence bug: batch
timeout previously overwrote an authenticated per-test verdict. Coverage now
preserves the same per-test evidence as ordinary execution, with a focused
regression covering pass, fail, timeout, and inconclusive terminal records. Cocotb
signal termination and this timeout fixture are inconclusive at the individual
test level: no complete XML or confirmed active-test record was published.
The batch still records its timeout. Generated/custom-main signal termination
is failure, and their timeout cases report timeout. Missing
native databases remain explicit collection errors and never replace the
simulation verdict. Custom-main fixtures write coverage before failing or
hanging, also proving that successful collection cannot promote a failed test.

Each pair retains `verdicts.json`; CI uploads it alongside the native artifacts.
The signal cleanup regression starts a process that ignores SIGTERM, requires
forced kill/reaping, and verifies the original timeout still fails the check.
All three native harness checks share one per-instance identity assertion.

GTKWave was installed only in a disposable validation container derived from
the candidate; it is not a new runtime dependency. Reproduce with GTKWave,
Xvfb, and xauth installed, using:

```bash
bash tests/docker/verilator_viewer_acceptance.sh /tmp/viewer-evidence
```

The script compiles the repository's `viewer.sv` with the pinned Verilator
and `--trace-fst`, then opens the result in the actual GTKWave GUI under Xvfb.
Its Tcl script adds both signals to the waveform view and queries decoded
values, forcing waveform-data reading beyond hierarchy loading:

- Viewer: GTKWave Analyzer v3.3.116.
- Hierarchy: `viewer.clk`, `viewer.counter[3:0]`; two signals, no aliases.
- Time range: 0 through 20; at time 5, clock is 1 and counter is 3.
- Measured FST SHA-256:
  `c9166f2518aedfd59a629629562142577ddbbe3d00aa89c612048664db4870fc`.

The script retains compiler/viewer versions, compiler source identity, build
and simulation logs, native FST, checksum, and GTKWave's successful assertion
output. GTKWave exits with a failed check on unexpected hierarchy, range, or
values; an outer timeout also bounds startup and reading.
