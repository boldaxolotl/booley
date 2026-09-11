# Coverage Campaign public release gate

Canonical coverage terminology is defined in the
[Simulation Coverage glossary](../../src/booley/flows/sim/CONTEXT.md).

Issue [#213](https://github.com/boldaxolotl/booley/issues/213), phase 7, exposes
Simulation `--coverage` / permanent `--cov`, MCP boolean `coverage`, and the
`coverage` Criterion alongside the exact-path, report-driven Coverage Analyst.
Phases 4–6 supply canonical persistence, exact pruning, and Analyst isolation.
The release does not introduce a waveform scorer or approve Waiver Candidates.

## Required verification

| Gate | Executable evidence |
|---|---|
| Interactive Mode | `tests/flows/sim/test_coverage_flow.py`: collect through `SimulateFlow`, then invoke the Analyst with the produced exact path; model transport is substituted and project bytes remain unchanged. |
| Ticket Mode | The same suite checks independent persisted Simulation/Coverage verdicts for pass/pass, fail/pass, pass/fail, fail/fail, and collector-blocked combinations. Transaction fault tests cover every publication boundary. |
| Campaign V3 storage | `tests/flows/sim/test_coverage_campaign_store.py` checks summary-only reads, deterministic source rollups, exact deep-load equivalence, the V1/V2 hard cutoff, resource ceilings, tamper rejection, safe paths, and create-if-absent publication. Retention and Analyst tests require the integrity-linked point store. |
| Public contracts | CLI aliases and help, MCP boolean schema, exposed Criterion catalog, generated references, docs-schema tests, and transport schema fixture. |
| Python | Full `tests/` suite with the hosted platform/marker matrix; inspect all skips. |
| Quality | `ruff check src/ tests/`, `ruff format --check .`, and `pyright`, using the exact pinned quality tools. |
| Packages | Build wheel and sdist; `twine check`, payload rejection, and installed-artifact validation for direct and sdist-built wheels. |
| Runtime Image | `tests/docker/verilator_acceptance.py`, release matrix, production collector smoke, and native FST cross-validation. |

## V2 storage characterization, 10 SEP 2026

A deterministic 40,000-point campaign exercises the checked-in structural scale test without
machine-specific timing thresholds. On the local Python 3.14 host it produced a 3,810-byte V2
manifest, a 970,291-byte compressed point store, and 26,180,081 decompressed point bytes. The
equivalent full Analyst request was 27,543,123 bytes and its empty-advisory output was 26,183,483
bytes, confirming that bounded Analyst evidence remains separate follow-up work.

With `tracemalloc`, summary reads took 0.0024 seconds and 16,910,994 peak traced bytes for the
one-point fixture versus 0.0029 seconds and the same peak for 40,000 points. The 40,000-point deep
load took 26.27 seconds and 205,726,188 peak bytes; full Analyst composition took 29.68 seconds and
175,639,433 peak bytes. These figures characterize one host rather than define portable budgets;
the structural test requires near-constant manifest size, summary reads with no point-store access,
lossless deep loading, and compression below the serialized Analyst payload.

V3 intentionally adds source-file rollups to the manifest, so manifest size now scales
with the number of distinct source paths rather than the number of Coverage Points. A
16 MiB publication and read ceiling bounds that growth.

## Validated query characterization, 11 SEP 2026

Issue #489 was measured on Linux 7.0 x86-64 with Python 3.14.4 and an Intel i7-14650HX.
The before source was `fa19f0d1`; the after source used the rebased issue branch. The standalone
`tests.performance.coverage_query_profile` controller created deterministic Campaigns with four
metrics, covered and uncovered incidence, eligible points, and full Approved Waiver provenance.
It then ran each measured sample in a fresh process. Each row is the median of three samples; time
is diagnostic and is not a CI threshold.

```console
PYTHONPATH=<revision>/src:<after-worktree> python3 -m \
  tests.performance.coverage_query_profile --points <2000|10000|40000> --samples 3
```

| Points | Phase | Before | After |
| ---: | --- | ---: | ---: |
| 2,000 | process peak RSS | 67,196 KiB | 56,208 KiB |
| 10,000 | process peak RSS | 192,288 KiB | 148,936 KiB |
| 40,000 | process peak RSS | 666,008 KiB | 492,880 KiB |
| 2,000 | session peak traced bytes | 3,492,490 | 222,250 |
| 10,000 | session peak traced bytes | 17,418,524 | 1,005,548 |
| 40,000 | session peak traced bytes | 70,010,236 | 4,312,876 |
| 2,000 | exact-ID seconds | 0.001542 | 0.000214 |
| 10,000 | exact-ID seconds | 0.008042 | 0.000222 |
| 40,000 | exact-ID seconds | 0.033669 | 0.000233 |
| 2,000 | second filtered page seconds | 0.034614 | 0.033025 |
| 10,000 | second filtered page seconds | 0.047191 | 0.033049 |
| 40,000 | second filtered page seconds | 0.094964 | 0.033123 |

At 40,000 points the validated session's peak traced allocation fell 93.8%, construction fell from
0.656 to 0.131 seconds, and exact-ID retrieval no longer grew with Campaign size. Repeated filtered
pages reuse one bounded match set: after its first scan, the second page stayed near 0.033 seconds
across all three sizes instead of growing to 0.095 seconds. Consuming loader-owned point dictionaries
only after all validation succeeds also lowered the 40,000-point deep-load traced peak from
274,217,099 to 195,528,847 bytes. Full loading and the first filtered scan remain linear; the result
does not establish constant-memory analysis.

The hosted `bwave-smoke` job runs both native pytest suites and checks their
JUnit report with `--min-tests 18 --max-skips 0`. Missing native prerequisites
cannot silently pass the release gate. The compiler acceptance runner independently
fails on missing tools or wrong source identity.

## Local evidence, 09 SEP 2026

Validation starts from main `5a45b4e8bdf2319335e956cd92660e8263cdee15`.
The local Runtime Image is `booley-sandbox:issue-419` (image prefix
`25d35ba51cab`), with this worktree mounted read-only and `PYTHONPATH=/work/src`.
Its exact Verilator `v5.052` source identity is
`ea338be98e1e838d3518809ce8899f85a009963c`. Test plugins are installed only in
disposable containers. This validates current source against an existing image;
the hosted candidate-image gate must also validate the newly packaged image.

- Compiler/native compatibility runner: **9 passed**, no skips, including 15
  ordinary/coverage verdict pairs across generated, custom, and Cocotb harnesses.
- Real release matrix plus production collector smoke: **18 passed**, no skips.
- Interactive collection followed by exact-path analysis: **passed**.
- Five Ticket verdict combinations: **passed**.
- Ordinary Cocotb Icarus and Verilator Flows: **3/3 tests passed on each**.
- Native FST cross-validation: authored harness and **28 command pairs passed**.
- Full Linux Python 3.14 suite: **10,615 passed, 64 explicitly skipped**, no
  failures or errors (92.51 seconds, four workers).
- Docs/schema and generated-reference follow-up: **38 passed**.
- Pinned Ruff lint/format and Pyright: **passed**.
- Wheel/sdist build, Twine metadata, and distribution payload checks: **passed**.
- Clean installed-artifact validation, direct wheel and sdist-built wheel: **passed**.
- Installed wheel exposes boolean collection and `sim --coverage` Criterion guidance.

Local evidence is retained in `/tmp/booley-phase7-validation`: `native.xml`,
`compiler/`, `fst.log`, `final-unit.xml`, `pyright.log`, build logs, and installed
file inventories. Native tests ran against current source in the Runtime Image;
those tests intentionally skip on a host without the EDA binaries.

The 64 host skips are accounted for: 18 native coverage tests passed separately
in the Runtime Image; 19 require a host B-Wave binary; 8 require an embedding
project; 5 require approved Vivado/license setup; 7 require opt-in sidecar images;
3 require a production image with PDK; 2 require the configured Codex CLI probe;
and 2 require Windows path or NTFS-junction semantics. No native release test skipped in its
required Runtime Image run.

Local source and package validation is green. Hosted Linux/Windows and rebuilt
candidate-image jobs require a pushed revision and are not claimed by local
execution. Public release remains contingent on those hosted checks; no branch,
PR, image, or release was published by this phase's local validation.

## Hosted seed-test correction

The first hosted native matrix exposed a test-only nondeterminism: its
same-seed check compared complete Verilator stdout, including wall-clock
telemetry (`0.000` versus `0.002` seconds). The emitted random value agreed.
The gate now requires exactly one eight-digit `RANDOM` value per run, compares
those values for identical seeds, and requires a different value for the other
seed. Native collection and all other release assertions remain unchanged.
