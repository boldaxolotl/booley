# Doctor diagnostics and shared health

How Doctor's checks are split across owners, what each owner may touch, and how
Doctor, MCP and the Dashboard read one shared health observation. The import
rules that keep these owners apart are in
[the source-dependency contract](SOURCE-DEPENDENCY-CONTRACT.md#doctor-diagnostic-boundary);
waiver syntax is in [DOCTOR.md](../user/DOCTOR.md#doctor-waivers-doctor-waiverstoml).

## Diagnostic owners

Doctor keeps check selection, phase ordering, rendering, waiver application and
deep Flow execution. Everything else is observed by an owner that accepts domain
inputs and returns a complete typed report:

| Owner | Public operations | Knowledge hidden from Doctor |
| --- | --- | --- |
| `runtime.inspection` | `inspect_runtime(RuntimeInspectionRequest)`; `inspect_retained_resources(project_root, docker_exe)` | Spec parsing and drift order, authority validation, host/runtime distinction, mounted Vivado policy, Docker inventories, exact issuance matching, relay topology, retained-resource classification. |
| `harness.host_diagnostics` | `inspect_host()` | Python/package/legacy-distribution checks, Bootstrap CHECK interpretation, Docker readiness, host clock and runtime gating. |
| `harness.setup.readiness` | `load_project(root)`; `check_guidance(project, mode=...)`; `check_stealth_cores(project, mode=...)` | Checkout-local Project resolution, schema composition, parsed EDA inputs, valid continuation data, guidance and core-projection grading and bounded reconciliation. |
| `audit.host_environment` | `inspect_agent_installation(provider)` | The shared configuration-directory-or-executable installation heuristic. |
| `runtime.interactive_docker` | `image_exists(name, executable=..., timeout=...)` | The image observation shared by Doctor and Project Initialization. |
| `audit.diagnostic_results` | `DiagnosticFinding`, `DiagnosticReport`, `HealthAssessment` | Ordered severity/message/remediation/ID/subject/deduplication values, uncounted verbose details, and the active FAIL/WARN interpretation. |

`RuntimeInspectionResult` separates configuration currency from issuance
validity: a stale convenience setting never suppresses the authority
observation. A report is evidence at an instant and grants no execution
authority; startup validates independently through the same issuance owner.

Every WARN and WAIVED finding requires a stable check ID at construction.
Doctor's single report adapter preserves fields, applies waivers and
deduplication, and interleaves verbose details without changing counts.

## Permitted effects

| Operation | Permitted effects and bounds |
| --- | --- |
| Runtime inspection | Reads Project/spec state; invokes bounded Git/Docker probes; calls authoritative validation. With active EDA, validation may create the private authority directory and acquire/create its lock. It does not issue, provision, repair, or start the persistent Sandbox. No-active-EDA validation does not require writable authority state. |
| Runtime version check | Runs a temporary `docker run --rm --pull=never --network none` version probe with a 30-second CLI timeout. A CLI timeout does not prove remote container cleanup. |
| In-runtime validation | Checks runtime isolation and mounted Vivado locally (90-second probe timeout), without reading host-private authority. |
| Retained-resource inspection | Lists state volumes and image keepers; does not delete them. |
| Host inspection | Uses Bootstrap `Intent.CHECK` and bounded environment probes; installs and repairs nothing. Host preparation/clock probes are skipped inside the Sandbox. |
| Project loading / INSPECT | Reads and audits; does not repair guidance or projections. |
| Project RECONCILE | Uses the guidance-link and core-projection owners to repair generated artifacts. It does not run Init or issue a Runtime. |

Automatic (read-only) Doctor uses INSPECT; manual Doctor keeps its bounded
repairs. Invalid configuration retains the resolved Booley Directory for later
checks while skipping operations that require validated Project configuration.

## Shared health

Doctor, MCP and the Dashboard consume one health capability that lives outside
command orchestration, so none of them imports the Doctor worker to learn
whether the machinery is healthy:

| Owner | Interface | Responsibility |
| --- | --- | --- |
| `audit.diagnostic_results` | `DiagnosticReport.active_findings`, `DiagnosticReport.health` | FAIL and WARN are active. FAIL gives exit 1; WARN without FAIL gives exit 0 but is not clean. Waived warnings are inactive. |
| `audit.health` | `HealthSnapshot`, `parse_diagnostics`, `finding_documents`, `freshness_reason` | The persisted finding shape, validation of a stored report, and the freshness decision from explicit drift/time inputs. Pure: no filesystem, Runtime, Harness or SDK dependency. |
| `audit.doctor_waivers` | `DoctorWaivers`, `warning`, `load_doctor_waivers` | Stable warning identities and Project waiver policy. Doctor applies waivers and deduplication before publishing. |
| `runtime.health` | `read_health`, `due_reason`, `publish_result`, `consume_changed_snapshot` | Fingerprint discovery, one-report snapshots, atomic report/transcript writes, and locked per-channel announcement state. Never executes Doctor. |
| `runtime.health_presentation` | `brief_message`, `current_summary`, `consume_changed_summary` | Brief text from a typed snapshot. Formatting never reloads the report. |
| `runtime.doctor_stamp` | `record_clean_run`, `check_stamp_advisory` | The independent last-clean stamp and its package/config/age advisory. |
| `harness.auto_doctor` | `launch`, `run_if_due` | Detached worker launch, run lock, read-only Doctor execution, crash capture and progress. |

### Reading an observation

`read_health` reads `runtime/doctor/last.json` once, validates it, then computes
its freshness. `publish_result` derives counts, `clean` and `exit_code` from the
findings, so a report Booley wrote cannot contradict itself.

A report is **unavailable** — not healthy, and due for a recheck — when it is
missing, unparsable, has an unsupported schema or an invalid field, or when its
active counts, `clean` or `exit_code` disagree with its findings. Reports
without the optional fields (`schema`, `clean`, `exit_code`, `deep_status`,
`finding_hash`) stay readable.

### Two records, two purposes

- `runtime/doctor/last.json` is the **last observation**. Automatic runs and
  complete manual in-Sandbox runs publish it, healthy or not. A partial manual
  profile never replaces it.
- `runtime/doctor_stamp.json` is the **last clean run**, written only by
  qualified clean runs.

Deep status is stored as metadata beside the findings: it affects neither the
active counts nor the announcement identity. A design that fails lint or
simulation correctly is a healthy machine reporting an unhealthy design.

### Freshness

Input drift is checked first: any change to the Doctor fingerprint (config,
guidance, core files, Booley version, upgrade-review state) makes the
observation due. Then age:

| Observation | Expires after |
| --- | --- |
| Clean, or only upgrade-review findings | 7 days |
| Any other FAIL or WARN | 1 day |
| Unreadable timestamp | immediately |

The clean stamp keeps its own whole-day `> max_age_days` threshold and
version-drift guidance.

### Announcements

Each channel (`session-up`, `mcp-tool`, …) remembers the identity of the last
observation it announced and reports a changed one once. Channels are
independent. Readers and renderers never write announcement state; only
`consume_changed_snapshot` does.

An `issues_only` channel does not record clean observations. So issue A, then
clean, then the same issue A stays silent on that channel; a channel that
consumes clean observations announces the recurrence.

The Dashboard reads one non-consuming snapshot and renders compact active FAIL
and WARN counts, or `Doctor: OK` when clean. It labels unavailable results and
ends a due result with ` · recheck due`, drawn in the attention color while the
counts keep their severity style. The separate `brief_message()` adapter
retains verbose findings and transcript paths for existing CLI and MCP callers.
