# Coverage Campaign public release gate

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
| Public contracts | CLI aliases and help, MCP boolean schema, exposed Criterion catalog, generated references, docs-schema tests, and transport schema fixture. |
| Python | Full `tests/` suite with the hosted platform/marker matrix; inspect all skips. |
| Quality | `ruff check src/ tests/`, `ruff format --check .`, and `pyright`, using the exact pinned quality tools. |
| Packages | Build wheel and sdist; `twine check`, payload rejection, and installed-artifact validation for direct and sdist-built wheels. |
| Session Image | `tests/docker/verilator_acceptance.py`, release matrix, production collector smoke, and native FST cross-validation. |

The hosted `bwave-smoke` job runs both native pytest suites and checks their
JUnit report with `--min-tests 18 --max-skips 0`. Missing native prerequisites
cannot silently pass the release gate. The compiler acceptance runner independently
fails on missing tools or wrong source identity.

## Local evidence, 09 SEP 2026

Validation starts from main `5a45b4e8bdf2319335e956cd92660e8263cdee15`.
The local Session Image is `booley-sandbox:issue-419` (image prefix
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
file inventories. Native tests ran against current source in the Session Image;
those tests intentionally skip on a host without the EDA binaries.

The 64 host skips are accounted for: 18 native coverage tests passed separately
in the Session Image; 19 require a host B-Wave binary; 8 require an embedding
project; 5 require approved Vivado/license setup; 7 require opt-in sidecar images;
3 require a production image with PDK; 2 require the configured Codex CLI probe;
and 2 require Windows path or NTFS-junction semantics. No native release test skipped in its
required Session Image run.

Local source and package validation is green. Hosted Linux/Windows and rebuilt
candidate-image jobs require a pushed revision and are not claimed by local
execution. Public release remains contingent on those hosted checks; no branch,
PR, image, or release was published by this phase's local validation.
