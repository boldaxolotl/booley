# Coverage release gate

Where Booley's native Simulation coverage is verified: collection
(`--coverage`, alias `--cov`, and the MCP boolean `coverage`), the `coverage`
Criterion, and the [Coverage Analyst](../../src/booley/flows/sim/GLOSSARY.md).
Read the first section before you change coverage code or cut a release that
touches it. The remaining sections are dated evidence from the September 2026
work that made coverage public (issue
[#213](https://github.com/boldaxolotl/booley/issues/213), phase 7) and from the
query-memory work in issue
[#489](https://github.com/boldaxolotl/booley/issues/489); they record what was
measured then and are not updated. Coverage terms are defined in the
[Simulation Coverage glossary](../../src/booley/flows/sim/GLOSSARY.md); the
design lives in [FLOW_IMPLEMENTATION.md](FLOW_IMPLEMENTATION.md) and the
Analyst's evidence boundary in
[MCP-TOOLS.md](SPECIALIST_IMPLEMENTATION.md#coverage-analyst).

## Required verification

| Area | Executable evidence |
|---|---|
| Interactive Mode | `tests/flows/sim/test_coverage_flow.py`: collect through `SimulateFlow`, then run the Analyst on the produced exact path. The model transport is substituted and project bytes stay unchanged. The same file checks independent Simulation and coverage verdicts and collector and publication failures. |
| Goal Mode | `tests/goals/test_translate.py` (simulation and coverage Goal translation), `tests/goals/test_recorder.py` (bound evidence publication, transaction recovery, complete coverage detail), and `tests/goals/test_simulation_coverage_verdicts.py` (every pass/fail simulation × coverage combination through the Goal adapter). Neither `pytest tests/` nor CI collects `tests/goals/` until Goal Mode is removed ([#3276](https://github.com/boldaxolotl/booley/issues/3276)), so run `pytest tests/goals` by hand. |
| Record storage | `tests/flows/sim/test_coverage_campaign_store.py`: deterministic source rollups, exact deep-load equivalence, rejection of every schema except V5, the uncompressed-size ceiling, malformed-store rejection, safe paths, and create-if-absent publication. |
| Public contracts | CLI aliases and help, the MCP boolean schema, the exposed Criterion catalog, generated references, and the docs and transport schema tests. |
| Sandbox Image | `tests/docker/verilator_acceptance.py`, then the release matrix, production collector, and compiler-cache smoke tests, then native FST cross-validation; all run in CI's `bwave-smoke` job. |

The `bwave-smoke` coverage-release step checks its JUnit report for at least
one test and no skips (`--max-skips 0`). The native tests skip on a host
without the EDA binaries, so without this check a missing prerequisite would
pass the gate silently. `verilator_acceptance.py` separately fails on a
missing tool or the wrong Verilator source identity. The general Python suite,
Ruff, Pyright, and package checks run in the standard CI lanes described in
[`.github/ci/README.md`](../../.github/ci/README.md).

## Phase 7 local evidence, 09 SEP 2026

Validation started from main `5a45b4e8bdf2319335e956cd92660e8263cdee15`. The
local Sandbox Image was `booley-sandbox:issue-419` (image prefix
`25d35ba51cab`), with the worktree mounted read-only and
`PYTHONPATH=/work/src`. Its Verilator `v5.052` source identity was
`ea338be98e1e838d3518809ce8899f85a009963c`. Test plugins were installed only in
disposable containers. This validated current source against an existing
image; the hosted candidate-image gate also had to validate the newly packaged
image.

- Compiler and native compatibility runner: **9 passed**, no skips, including
  15 ordinary/coverage verdict pairs across generated, custom, and Cocotb
  harnesses.
- Real release matrix plus production collector smoke: **18 passed**, no skips.
- Interactive collection followed by exact-path analysis: **passed**.
- Five Ticket verdict combinations: **passed**.
- Ordinary Cocotb Icarus and Verilator Flows: **3/3 tests passed on each**.
- Native FST cross-validation: authored harness and **28 command pairs passed**.
- Full Linux Python 3.14 suite: **10,615 passed, 64 explicitly skipped**, no
  failures or errors.
- Docs, schema, and generated-reference follow-up: **38 passed**.
- Pinned Ruff lint and format, and Pyright: **passed**.
- Wheel and sdist build, Twine metadata, and distribution payload checks:
  **passed**.
- Clean installed-artifact validation, direct wheel and sdist-built wheel:
  **passed**; the installed wheel exposed boolean collection and the
  `sim --coverage` Criterion guidance.

The 64 host skips were accounted for: 18 native coverage tests passed
separately in the Sandbox Image; 19 required a host B-Wave binary; 8 an
embedding project; 5 approved Vivado and license setup; 7 opt-in sidecar
images; 3 a production image with a PDK; 2 the configured Codex CLI probe; and
2 Windows path or NTFS-junction semantics. No native release test skipped in
its Sandbox Image run. Raw logs stayed on the validating host. Hosted Linux,
Windows, and rebuilt candidate-image jobs needed a pushed revision and are not
covered by this local run.

The first hosted native matrix then exposed a test-only nondeterminism: the
same-seed check compared complete Verilator stdout, which includes wall-clock
telemetry (`0.000` versus `0.002` seconds), while the random value agreed. The
check was changed to require exactly one eight-digit `RANDOM` value per run,
equal values for identical seeds, and a different value for the other seed.

## Storage characterization, 10 SEP 2026

A deterministic 40,000-point Coverage Record (then the V2 format) exercised the
checked-in structural scale test. On the local Python 3.14 host it produced a
3,810-byte manifest, a 970,291-byte compressed point store, and 26,180,081
decompressed point bytes. The equivalent full Analyst request was 27,543,123
bytes and its empty-advisory output 26,183,483 bytes, which showed that bounded
Analyst evidence still needed separate work.

With `tracemalloc`, the 40,000-point deep load took 26.27 seconds and
205,726,188 peak bytes; full Analyst composition took 29.68 seconds and
175,639,433 peak bytes. These figures describe one host, not portable budgets;
the structural test requires near-constant manifest size, lossless deep
loading, and compression below the serialized Analyst payload.

The later V3 format added source-file rollups to the manifest, so manifest size
grows with the number of distinct source paths rather than the number of
Coverage Points. A 16 MiB publication and read ceiling bounds that growth.

## Validated query characterization, 11 SEP 2026

Issue #489 was measured on Linux 7.0 x86-64 with Python 3.14.4 and an Intel
i7-14650HX. The before source was `d7b67321`; the after source was the
review-fixed issue branch. The standalone
`tests.performance.coverage_query_profile` controller created deterministic
records with four metrics, covered and uncovered incidence, eligible points,
and full Approved Waiver provenance. Each sample runs in a fresh analyzer
process that keeps its deep-loaded record while a fresh, isolated evidence
process deep-loads and queries the same record. The runner samples their
combined Linux RSS every 10 milliseconds and records each process separately.
Each row is the median of three samples; time is diagnostic and is not a CI
threshold.

```console
PYTHONPATH=<revision>/src:<after-worktree> python3 -m \
  tests.performance.coverage_query_profile --points <2000|10000|40000> --samples 3
```

| Points | Phase | Before | After |
| ---: | --- | ---: | ---: |
| 2,000 | process-tree peak RSS | 128,944 KiB | 111,276 KiB |
| 10,000 | process-tree peak RSS | 354,292 KiB | 274,940 KiB |
| 40,000 | process-tree peak RSS | 1,202,148 KiB | 886,820 KiB |
| 2,000 | session peak traced bytes | 3,492,490 | 222,250 |
| 10,000 | session peak traced bytes | 17,418,524 | 1,005,548 |
| 40,000 | session peak traced bytes | 70,010,236 | 4,312,876 |
| 2,000 | session construction seconds | 0.030507 | 0.006508 |
| 10,000 | session construction seconds | 0.154634 | 0.033879 |
| 40,000 | session construction seconds | 0.655418 | 0.132107 |
| 2,000 | exact-ID seconds | 0.001645 | 0.000225 |
| 10,000 | exact-ID seconds | 0.008344 | 0.000250 |
| 40,000 | exact-ID seconds | 0.032358 | 0.000238 |
| 2,000 | second filtered page seconds | 0.035413 | 0.033327 |
| 10,000 | second filtered page seconds | 0.048089 | 0.033536 |
| 40,000 | second filtered page seconds | 0.094023 | 0.033168 |

At 40,000 points the process-tree peak fell 26.2%, the validated session's
peak traced allocation fell 93.8%, construction fell from 0.655 to 0.132
seconds, and exact-ID retrieval stopped growing with record size. Repeated
filtered pages reuse one bounded match set: after its first scan, the second
page stayed near 0.033 seconds at all three sizes instead of growing to 0.094
seconds. Consuming loader-owned point dictionaries only after all validation
succeeds also lowered the 40,000-point deep-load traced peak from 274,216,879
to 195,528,575 bytes. Full loading and the first filtered scan remain linear;
the result does not establish constant-memory analysis.
