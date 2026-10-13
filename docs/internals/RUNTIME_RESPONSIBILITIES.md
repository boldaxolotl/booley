# Runtime responsibilities and Job observation

The responsibility map for #1296 was investigated at
`2813cb2d952486995d3e79503dfdc5b1fad517d0` after #1293's Job lifecycle and report
encoding work merged. It covered all 117 Python modules then in Runtime. The
groups below describe behavior, not a proposed wholesale directory layout.

## Responsibility map

| Responsibility | Principal existing owners | Representative production consumers | Disposition |
| --- | --- | --- | --- |
| Sandbox issuance and lifecycle | `session_issuance`, `session_spec`, `session_runtime`, `session_refresh`, admission and lifecycle locks | Project Initialization, Sandbox commands, refresh, Doctor, attachment | Keep issuance, authority, live Sandbox replacement and recovery with their existing owners. A directory grouping alone would not simplify callers. |
| Sandbox Images and provenance | `image_lifecycle`, `image_provenance`, `image_identity`, `project_image`, `host_base_acquisition` | Bootstrap, Initialization, refresh, Doctor | Keep the existing reconciliation and transaction interface, respecting ADR 0053's separation of image and live Sandbox ownership. |
| Supervised execution and process observation | `runtime_attachment`, `execution_supervisor`, `execution_records`, `execution_recovery`, `pid`, `process_group` | Sandbox commands, Flow execution, MCP, Specialists, Dashboard resource sampling | Preserve descendant cleanup, durable execution identity and recovery. A broader extraction needs evidence beyond import counts; supervisor module launches are also executable contracts. |
| Jobs and endpoint execution | `job_records`, `job_slots`, `job_wait`, `endpoint_execution`, `job_observation` | MCP lifecycle, Flow execution, Goal finalization, Dashboard, cleanup | Extract only read-only Job observation. Record persistence, admission and execution remain distinct behavioral owners. |
| Agent execution and provider adapters | `agent`, `agent_backend`, `agent_config`, provider backends, transcript and prompt artifacts | Specialists, developer probe, MCP composition | Preserve the existing provider seam and capability policy. Moving the adapter files would not itself reduce caller knowledge. |
| Project and Git/worktree mechanics | `project_dir`, `project_repositories`, pairing, relocation, materialization and Git helpers | Goal entry/finish, Flow baselines, Initialization, worktree commands | Reuse stable Project discovery and repository-coordinate owners; do not introduce a second Project resolver. |
| Initialization and attachment integrations | `init_plan`, `dashboard_tasks`, `skill_links`, in-container integrations, `shared_infra` | Initialization, guidance reconciliation, editor attachment, Flow support | Keep concrete file ownership and conditional recovery local. An umbrella forwarding interface would hide little behavior. |
| Health, activity and presentation support | `health`, `inspection`, `session_activity`, `health_presentation`, artifact paths, timestamps | Doctor, automatic diagnostics, Dashboard, producers | Reuse the shared health interface delivered by #1294 and the reported-activity contract. |
| Filesystem publication and compatibility primitives | `atomic_files`, `publication_ownership`, `regular_file`, `safe_storage`, storage/locking compatibility imports | Durable record owners, Goal publication, Flow artifacts, Initialization | Keep atomicity and path-safety mechanics beneath caller policy. A generic utilities package is not a behavioral extraction. |

`runtime.__init__` was the remaining package marker. The selection deliberately
leaves #1293's `JobLifecycle` and `ReportEncoder`, #1295's Simulation orchestration,
and #1406's endpoint preparation/lifecycle work with their respective owners.

## Selected interface

`booley.runtime.job_observation.JobObserver` owns one Project's read-only Job
refreshes. Construct it with the explicit Project directory, optional slot/proc
roots, and an optional validator for caller-owned metadata. Call `read(roots)`
with the ordered roots selected by composition. It returns neutral observations
and isolated diagnostics.

The implementation owns record scan and projection limits, newest-record
selection, conservative process/slot states, exact report/progress lookup,
available artifact paths, endpoint cache capacity, and successful refresh
generations. Callers do not construct a cache or coordinate `configure_project`,
`begin`, `prepare_endpoints`, reads and `complete`. A failed observation does not
commit its touch generation. Observation never polls, repairs, reaps or writes a
Job; run identity includes its retained root.

`harness.dashboard.jobs.JobReader` owns the higher-level composition. It selects
roots from the same Goal enumeration that supplied the Dashboard's Goal rows:
the explicit current interactive root, all retained Goal Records, then the newest
64 historical interactive roots. It validates optional Goal bindings before a
record reaches process/artifact projection and enriches observations with
Target-derived EDA labels. Dashboard resource sampling and checking-Goal
association remain in the Dashboard model. UI code consumes prepared values.

This improves an already useful interface: the former `snapshot_jobs` function
already hid cache method ordering. The extraction removes the cache type and
lifetime from DashboardReader, eliminates its second Goal enumeration, and
removes Goal/Target interpretation from the reusable observation implementation.
It is not justified merely by replacing a function with a class.

Deleting JobObserver would put bounded scanning, record validation, slot/process
projection, artifact joins, path safety, cache generations and diagnostics back
into the Dashboard composition. Deleting a forwarding facade alone would restore
none of those obligations; that is the deletion test for this extraction.

## Dependency and compatibility decisions

Runtime no longer imports Goals. D36 protects this direction. D37 keeps the Job
observation package independent of Targets; D5/D6 already protect Runtime from
MCP, Specialists and Harness. No composition exemption or cycle expansion is
needed. Runtime still consumes `targets.flow_names` through `shared_infra`.

The observer reuses the released read-only progress functions from
`flows.progress_lifecycle`. That owner also contains publication, repair,
supersession, locking and coverage-origin validation. Moving it wholesale would
relocate unrelated behavior; choosing a shared lower-level progress owner needs
its own investigation. Thus Runtime's Flow dependency remains explicit in this
slice, including the existing complete exact-progress fallback past the parsed
artifact-history limit.

The former internal `runtime.job_snapshot` module is replaced by the observation
facade and Dashboard composition. All in-repository consumers migrate together;
CLI/MCP behavior, persisted record/report layouts and schemas remain unchanged.
`runtime.job_artifacts.JobArtifactCache` remains a compatibility import for
standalone cache callers. Dashboard code uses only the observer interface.

## Concrete test adapters

- Record, artifact and slot readers use real temporary files, controlled mtimes,
  invalid documents, and symlink/nonregular-path fixtures. No storage protocol is
  introduced for a hypothetical remote adapter.
- Process observation uses the existing synthetic proc-tree path seam; platform
  liveness and unavailable observations retain their focused fault-injection
  tests.
- Dashboard composition supplies the actual `GoalRunBinding` decoder. Observer
  tests supply deterministic validators that accept or reject unrelated metadata
  and prove rejection happens before artifact observation.
- Goal history and Target enrichment tests use real GoalStore and Target fixtures.
  Runtime tests exercise explicit roots without monkeypatching Goal discovery.
- Observer interface tests cover successful, failed and repeated generations.
  Internal artifact-cache tests remain for transient I/O, lexical guards,
  eviction, source-byte limits and fallback behavior that require fault injection.

The governing behavior is ADR 0068's read-only Dashboard and retained-history
contract. File fan-out remains diagnostic under #279; smaller directories and
lower counts alone do not establish a useful interface.
