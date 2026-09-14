# Doctor diagnostic interfaces: #532 evidence

This records the implementation of [#532](https://github.com/boldaxolotl/booley/issues/532)
and supplies before/after evidence for the architecture programme in
[#279](https://github.com/boldaxolotl/booley/issues/279). These measurements are
diagnostic evidence, not a numeric fan-out gate. The programme's separate
14 OCT 2026 decision date is unchanged.

## Interfaces and ownership

Doctor retains check selection, phase ordering, rendering, warning waivers,
counts, final health/exit status, reports, and deep Flow execution. The extracted
owners accept domain inputs and return complete typed observations:

| Owner | Public operations | Knowledge hidden from Doctor |
| --- | --- | --- |
| `runtime.inspection` | `inspect_runtime(RuntimeInspectionRequest)`; `inspect_retained_resources(project_root, docker_exe)` | Spec parsing and drift order, authority validation, host/runtime distinction, mounted Vivado policy, Docker inventories, exact issuance matching, relay topology, retained-resource classification. |
| `harness.host_diagnostics` | `inspect_host()` | Python/package/legacy-distribution checks, Bootstrap CHECK interpretation, Docker readiness, host clock and runtime gating. |
| `harness.setup.readiness` | `load_project(root)`; `check_guidance(project, mode=...)`; `check_stealth_cores(project, mode=...)` | Checkout-local Project resolution, schema composition, parsed EDA inputs, valid continuation data, guidance and core-projection grading and bounded reconciliation. |
| `audit.host_environment` | `inspect_agent_installation(provider)` | The shared configuration-directory-or-executable installation heuristic. |
| `runtime.interactive_docker` | `image_exists(name, executable=..., timeout=...)` | The image observation shared by Doctor and Project Initialization. |
| `audit.diagnostic_results` | `DiagnosticFinding`, `DiagnosticReport` | Ordered severity/message/remediation/ID/subject/deduplication values, plus uncounted verbose details. |

`RuntimeInspectionResult` separates configuration currency from issuance validity:
a stale convenience setting never suppresses the authority observation. A
report is evidence at an instant and grants no execution authority. Startup
continues to validate independently through the same issuance owner and Runtime
matching primitives.

Every diagnostic WARN requires a stable check ID at construction. Doctor's
single report adapter preserves fields, applies waivers and deduplication, and
interleaves verbose details without changing counts. Bootstrap PENDING previously
passed an unidentified string into the warning reporter and raised `TypeError`.
It now produces `host.bootstrap-pending`, with the Bootstrap resource as subject.
Malformed non-object devcontainer JSON likewise becomes a diagnostic failure
instead of raising while examining its fields. These are the intentional edge-case
fixes accompanying the extraction.

## Effects and sequencing

| Operation | Permitted effects and bounds |
| --- | --- |
| Runtime inspection | Reads Project/spec state; invokes bounded Git/Docker probes; calls authoritative validation. With active EDA, validation may create the private authority directory and acquire/create its lock. It does not issue, provision, repair, or start the persistent Session Runtime. No-active-EDA validation does not require writable authority state. |
| Runtime version check | Runs the existing temporary `docker run --rm --pull=never --network none` version probe with a 30-second CLI timeout. A CLI timeout does not prove remote container cleanup; this pre-existing limitation is unchanged. |
| In-runtime validation | Checks runtime isolation and mounted Vivado locally (90-second probe timeout), without reading host-private authority. |
| Retained-resource inspection | Lists state volumes and image keepers; does not delete them. |
| Host inspection | Uses Bootstrap `Intent.CHECK` and bounded environment probes; installs and repairs nothing. Host preparation/clock probes are skipped inside the Session Runtime. |
| Project loading / INSPECT | Reads and audits; does not repair guidance or projections. |
| Project RECONCILE | Uses existing guidance-link and core-projection owners to repair generated artifacts. It does not run Init or issue a Runtime. |

Project operations remain separate because actual observations and repairs are
interleaved with other Doctor checks. The order stays configuration, upgrade
review, guidance, worktree pruning, line endings, shadow guard, projections,
then board orphans. Invalid configuration retains the resolved Project directory
for later checks while skipping operations that require validated Project data.
Automatic/read-only Doctor uses INSPECT; manual Doctor retains its existing
bounded repairs. Deep Flow startup, configured timeouts, negative selftests,
cleanup/finalization, and output paths remain with their existing execution owners.

## Locality and deletion test

Deleting Runtime inspection would force Doctor to regain spec, issuance, mounted
Vivado, container-label, relay and retained-resource knowledge. A malformed-spec
case and authority-lock effects are now tested through the Runtime interface with
real sealed/issued temporary specs and deterministic Docker transport. No Doctor
implementation changes are needed for those policy cases.

Deleting readiness would return path resolution, schema ordering, continuation
data, and inspect/repair policy to Doctor. Guidance scope and stealth projection
cases now exercise public readiness operations. Init and readiness share the
existing guidance/projection repair owners, including the guidance-current
observation next to its writer.

Deleting host diagnostics would return Bootstrap health translation and
location-specific sequencing to Doctor. The old private Init agent-detection
helpers had no Init callers: they were removed, while Doctor's two consumers now
use the shared host observation. Init's configured-provider selection is
unchanged. Its image check and Doctor's image check share the Runtime observation.

Removed Doctor-private test targets include `_check_devcontainer_spec`,
`_check_issued_session_runtime`, `_check_interactive_state_volumes`,
`_check_issued_image_keepers`, `_check_project_setup`, `_validate_booley_toml`,
`_check_agents_md`, `_check_stealth_cores`, and `_run_host_checks`. Their cases move
to Runtime, readiness, or audit suites; test-only report adapters retain useful
message assertions. Doctor composition fixtures substitute complete results at
public seams where appropriate. Integration tests also execute real owners,
assert phase order and actual repair effects, and cover Bootstrap warning waivers
and clean-run stamps. This is not a patch-count target or a generic probe registry.

D1/D6 already protect audit and Runtime from Harness dependencies. D23 additionally
forbids the new Harness diagnostic owners from importing Doctor, Init command
orchestration, CLI composition, or rendering modules. Ordinary, deferred, and
`TYPE_CHECKING` imports are tested. The package SCC did not split, so its ratchet
metadata was not tightened.

## Reproducible dependency measurements

Measured on 14 SEP 2026 using each revision's `tests/architecture` analyzer:

- Before: `35062ddae5a60656dcd1b982904c214c74a7bca2` (`origin/main` at the merge-readiness rebase).
- After: `28b9997722f8b1c151d4e8b135972bd228e971aa` (rebased implementation with Windows fixture fix).

| Diagnostic | Before | After |
| --- | ---: | ---: |
| Python modules | 494 | 498 |
| Located dependency facts | 2,458 | 2,482 |
| Unique module edges | 2,017 | 2,042 |
| Direct mutual package pairs | 13 | 13 |
| Largest cyclic package group | 18 | 18 |

Fan-out is unique in-repository target modules, including deferred/type-only imports.
“New” means no baseline module; it is not a measured zero.

| Owner | Before | After |
| --- | ---: | ---: |
| `harness.doctor` | 66 | 57 |
| `harness.init_cmd` | 39 | 39 |
| `runtime.inspection` | New | 12 |
| `harness.host_diagnostics` | New | 6 |
| `harness.setup.readiness` | New | 15 |
| `audit.diagnostic_results` | New | 0 |
| `audit.host_environment` | 2 | 2 |
| `harness.bootstrap` | 8 | 8 |
| `harness.setup.guidance_links` | 3 | 3 |
| `harness.setup.docker_image` | 8 | 9 |
| `fusesoc.core_projection` | 1 | 1 |
| `runtime.interactive_docker` | 2 | 2 |
| `runtime.session_issuance` | 12 | 12 |
| `runtime.session_runtime` | 13 | 13 |
| `runtime.devcontainer` | 4 | 4 |

The total graph grows while Doctor loses policy knowledge: the new owners compose
existing mechanisms behind typed reports, and image observation gains an explicit
shared dependency. Counts alone do not establish depth; the caller knowledge,
shared policy and owner-local tests above are the evidence.

To reproduce, set `snapshot_ref` to either full revision above:

```sh
snapshot_dir="$(mktemp -d)"
git archive "$snapshot_ref" src/booley tests/architecture | tar -x -C "$snapshot_dir"
python3 "$snapshot_dir/tests/architecture/report.py" \
  --source-root "$snapshot_dir/src/booley" --top 500
```

The report omits sources with zero edges from its fan-out listing;
`audit.diagnostic_results` has no in-repository imports. Full reports include the
unchanged 18-member SCC and 13 mutual pairs.

## Validation and review

At `5874e7cf` on 14 SEP 2026, the full Python suite passed **11,289 tests**
with **65 skips**, using the repository virtual environment, local socket access,
and `pytest -q -n 4 --dist=loadscope --tb=short`. The required focused Doctor,
Init, Runtime, audit, guidance, release-host and architecture run passed 1,784
tests with one skip before the two small review fixes; the changed Runtime/Docker
suite passed 85 tests afterward. `ruff check src/ tests/` and `git diff --check`
passed.

Independent Standards and Spec reviews found no outstanding issues after review
fixes: JSON boundaries use `core.boundary.require_dict`, and the shared image
observation forwards its executable consistently. Retained-resource tests isolate
both inventories at the production interface, avoiding host Docker state.

The merge-readiness follow-up rebased onto current `main`, which includes the
updated confidential identity policy. The five branch commits passed a scan
against the sealed policy without local allowed-identity additions. The Runtime
filesystem fixture now compares `Path` objects, so its required and forbidden
paths match on Windows and POSIX. Runtime inspection and architecture tests
passed 192 tests after this change; Windows CI verifies the affected host path.

The changed-line coverage follow-up exercises mounted Vivado failures, runtime
isolation, retained image classification, unreadable Project data, host finding
translation, and guidance/projection repair boundaries through the production
interfaces. A local coverage run passed 3,829 tests with one skip; the final
focused additions passed 38 tests. Combined coverage of changed production
statements is 656/708 (92.66%), above the required 90% CI threshold.
