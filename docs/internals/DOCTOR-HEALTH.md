# Doctor diagnostics and shared health

This doc explains how Doctor's checks are split across owner modules, what
each owner may touch, and how Doctor, MCP, and the Dashboard read one shared
health observation. It is for contributors and agents adding or changing a
Doctor check, or reading Doctor results from new code. What users see and
waiver syntax are in [DOCTOR.md](../user/DOCTOR.md); the import rule that
keeps the owners apart (D26) is in the
[Source Dependency Contract](SOURCE-DEPENDENCY-CONTRACT.md#direction-rules).

The split exists so that each check's knowledge lives with the code it
checks, and so that MCP and the Dashboard can read Doctor's verdict without
importing the Doctor command.

## Diagnostic owners

Doctor keeps check selection, phase ordering, rendering, waiver application and
deep Flow execution. Everything else is observed by an owner that accepts domain
inputs and returns a complete typed report:

| Owner | Public operations | Knowledge hidden from Doctor |
| --- | --- | --- |
| `runtime.inspection` | `inspect_runtime(RuntimeInspectionRequest)`; `inspect_retained_resources(project_root, docker_exe)`; `inspect_interactive_git(InteractiveGitRequest)` | Spec parsing and drift order, authority validation, host/runtime distinction, mounted Vivado policy, Docker inventories, exact issuance matching, relay topology, retained-resource classification; Git `info/exclude` entry requirements (including Stealth names) and tracked `.interactive_logs` scratch. |
| `runtime.vaporview` | `inspect_viewer_bridge(ViewerBridgeRequest)` | In-Sandbox versus host probing, Docker-absent and no-devcontainer skips, extension-host detection, VaporView version, Viewer Bridge inventory, workspace-trust waiting, and the recovery for a dark bridge. |
| `harness.host_diagnostics` | `inspect_host()` | Python/package/legacy-distribution checks, Bootstrap CHECK interpretation, Docker readiness, host clock and runtime gating, read-only Session store identity scan. |
| `harness.setup.readiness` | `load_project(root)`; `check_guidance(project, mode=...)`; `check_stealth_cores(project, mode=...)`; `inspect_worktree_portability(WorktreePortabilityRequest)`; `inspect_line_endings(LineEndingInspectionRequest)` | Checkout-local Project resolution, schema composition, parsed EDA inputs, valid continuation data, guidance and core-projection grading and bounded reconciliation; worktree portability policy; line-ending observation precedence (CRLF, autocrlf risk, attributes policy, stale index, migration) and its check IDs and subjects. |
| `flows.sim.backends.cocotb_target_checks` | `inspect_cocotb_targets(CocotbTargetRequest)`; `project_has_cocotb_target(project_root)` | cocotb Target selection, the `select` template contradiction, module-file/fileset/`tb`-tag rules, the static AST test-name scan and generating-module deferral, the duplicate-section skip, and their check IDs and subjects. |
| `flows.target_test_plan_inspection` | `inspect_target_test_plans(TargetTestPlanInspectionRequest)` | `tests.toml` load and validation, section-key-to-Target matching (dead and duplicate keys), per-Target Test Run plan applicability and the seeding line each passing Target gets. |
| `audit.core_tree_inspection` | `inspect_core_tree(CoreTreeInspectionRequest)`; `inspect_core_security(CoreSecurityInspectionRequest)`; `owned_core_files(project_root, seed_targets)`; `fusesoc_rejected_cores(project_root)`; `rejected_target_core(project_root, target, rejected_cores)` | `.core` ownership (a core hosting a Doctor-selected Target, in the state zone, or with a misplaced `booley:` marker FAILs; a vendored core is a NOTE), FuseSoC's rejection verdict and the duplicate-row suppression, the provider-block rule over the dependency closure, recursive-symlink grouping with its verbose-only link paths, and the severity of each security violation (Scope-dependent kinds are advisory WARNs with a check ID). The request's `seed_targets` and `write_scope` are raw inputs Doctor derives. |
| `runtime.core_repository_hygiene` | `inspect_core_repository_hygiene(CoreRepositoryHygieneRequest)` | Which `.core`-referenced files must be tracked by Git, a Versioned Booley Directory judged by its own repository, symlink- and submodule-aware tracking, literal `$readmemh`/`$readmemb` target resolution, the build-artifact suffix and sibling-source rule with the `tags: [vendored]` exemption, and the check IDs (`project.core-file-untracked`, `project.memory-image-untracked`, `project.committed-build-artifact`) and sorted comma-joined subjects of their WARNs. |
| `runtime.core_resolution` | `inspect_core_resolution(CoreResolutionRequest) -> CoreResolutionResult` | Selecting each Doctor-selected Target through the catalog (a selection failure is that Target's FAIL), the in-Sandbox resolver snippet and its payload, verdict grading (exactly one bounded verdict per selected Target; missing, incomplete, duplicate or malformed evidence fails), Sandbox start-failure grading, the unavailable-runtime skip, and the always-shown error excerpt. Doctor keeps the `core.resolve.<hash>` deep-completeness identifiers and marks each returned selector complete. |
| `runtime.project_image` | `inspect_native_dependencies(NativeDependencyRequest)` | The curated header-to-Debian-package table, the C-family source scan and its file cap, and the rule for what counts as a Dockerfile `apt install` declaration. The request's `sources` are the selected Targets' compile inputs; the owner scans the C-family ones. |
| `audit.output_excerpt` | `output_excerpt_lines(text, max_lines)` | Which lines of failing command output are informative: the error-marker span plus the trailing reason line, capped, with a hidden-line note. |
| `audit.host_environment` | `inspect_agent_installation(provider)` | The shared configuration-directory-or-executable installation heuristic. |
| `runtime.interactive_docker` | `image_exists(name, executable=..., timeout=...)` | The image observation shared by Doctor and Project Initialization. |
| `audit.diagnostic_results` | `DiagnosticFinding`, `DiagnosticReport`, `HealthAssessment` | Ordered severity/message/remediation/ID/subject/deduplication values, uncounted details (verbose-only unless an owner marks one always shown), and the active FAIL/WARN interpretation. |

`RuntimeInspectionResult` separates configuration currency from issuance
validity: a stale convenience setting never suppresses the authority
observation. A report is evidence at an instant and grants no execution
authority; startup validates independently through the same issuance owner.

Every WARN and WAIVED finding requires a stable check ID at construction.
Doctor's single report adapter preserves fields, applies waivers and
deduplication, and interleaves details (verbose-only unless marked) without changing counts.

## Permitted effects

| Operation | Permitted effects and bounds |
| --- | --- |
| Runtime inspection | Reads Project/spec state; invokes bounded Git/Docker probes; calls authoritative validation. With active EDA, validation may create the private authority directory and acquire/create its lock. It does not issue, provision, repair, or start the persistent Sandbox. No-active-EDA validation does not require writable authority state. |
| Runtime version check | Compares the issued Sandbox Image's Booley build identity with the host installation by reading `docker image inspect` metadata (15-second timeout); starts no container. |
| In-runtime validation | Checks runtime isolation and mounted Vivado locally (90-second probe timeout), without reading host-private authority. |
| Retained-resource inspection | Lists state volumes and image keepers; does not delete them. |
| Host inspection | Uses Bootstrap `Intent.CHECK` and bounded environment probes; installs and repairs nothing. Host preparation/clock probes are skipped inside the Sandbox. |
| Project loading / INSPECT | Reads and audits; does not repair guidance or projections. |
| Line-ending inspection | Runs INSPECT-mode line-ending reconciliation with its bounded read-only Git probes; repairs nothing. |
| Interactive Git inspection | Runs bounded `git rev-parse --git-common-dir` (with its filesystem fallback) and `git ls-files` (10-second timeout), and reads `info/exclude`; writes nothing. |
| cocotb Target inspection | Reads `.core` files and the cocotb module source (parsed with `ast`, never imported or executed); writes nothing. |
| Viewer Bridge inspection | Runs bounded 20-second `python3 -c` probes in the attached VS Code devcontainer (`docker exec`) or locally inside the Sandbox, and reads the Sandbox inventory; writes nothing. |
| Test Run plan inspection | Reads `tests.toml` and the Project's `.core` files through `TargetCatalog`; runs no subprocess and writes nothing. |
| Core-tree inspection | Reads `.core` files and walks the Project tree for directory links; runs no subprocess and writes nothing. |
| Core repository hygiene | Reads `.core` files and the referenced HDL; runs bounded read-only `git ls-files` (15-second timeout) in the Project Root and a Versioned Booley Directory; writes nothing. |
| Deep core resolution | Builds `TargetCatalog` from the Project's `.core` files; runs one `python3 -c` resolver through the request's Sandbox command builder (600-second timeout), which may start the Project's Sandbox; the resolver writes FuseSoC build trees under the checkout's Booley Directory at `runtime/doctor/core-resolve/<key>`. |
| Native-dependency inspection | Reads the listed source files and the Project Dockerfile; starts no subprocess and writes nothing. |
| Project RECONCILE | Uses the guidance-link and core-projection owners to repair generated artifacts. It does not run Init or issue a Runtime. |

Automatic (read-only) Doctor uses INSPECT; manual Doctor keeps its bounded
repairs. Invalid configuration retains the resolved Booley Directory for later
checks while skipping operations that require validated Project configuration.

## Adding a diagnostic owner

A new owner, or a check moved out of `harness.doctor`, follows one
convention:

- The owner takes a typed, frozen request and returns a `DiagnosticReport` built
  with `audit.diagnostic_results.Findings`. Every WARN carries a `check_id`, plus
  `subject` and `dedupe` where the check had them. The owner accepts no Doctor
  reporting callbacks; a Sandbox command builder in the request is not one. The
  request carries raw inputs, and the owner decides ownership and severity.
- The owner imports no Doctor, CLI orchestration, or rendering module. Add
  its module to D26's sources in `tests/architecture/booley_contract.py` and
  in the D26 row of the
  [Source Dependency Contract](SOURCE-DEPENDENCY-CONTRACT.md#direction-rules).
  D26 covers owners that take a request and return a report; the single-fact
  helpers in the table (`audit.host_environment`,
  `runtime.interactive_docker`) and the value module
  `audit.diagnostic_results` stay outside it.
- Doctor builds the request, calls the owner, and renders each report with
  `_Reporter.diagnostics` at its check's position. Moving a check this way
  changes none of its message, fix text, severity, check ID, waiver behavior,
  order, or exit code.
- Tests assert on the returned report and patch no private Doctor name. Only
  tests about waivers, deduplication, `finish()` or the persisted Doctor record
  render the report through `_Reporter.diagnostics`.
- When an orchestrator inventoried in `tests/harness/test_doctor_probe_inventory.py`
  called the replaced check directly, the inventory names the owner call instead.
- An owner exposes one operation per position at which its checks render. When
  Doctor needs more than findings, an operation may return a result that wraps
  the report.
- The maintainer approved treating the rewrite of a moved check's reporting
  calls into `Findings`, with its text unchanged, as a mechanical move under
  [principle 18](CODING_PRINCIPLES.md#18-pull-requests-stay-small) of
  `CODING_PRINCIPLES.md`. The PR body states moved and new line counts.
- Sibling moves edit the same owner table, D26 sources and probe inventory, so
  they land one at a time, ordered by a `Blocked by` edge between the issues.
- Add the owner to the D26 test list in
  `tests/architecture/test_source_dependency_contract.py` first and watch it
  fail, then add it to the D26 sources in `tests/architecture/booley_contract.py`.
  Tests import a new owner inside a helper (`importlib.import_module`),
  so the file collects before the owner exists and each test fails on its own.

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
observation due. Then age (the user-facing summary is in
[DOCTOR.md](../user/DOCTOR.md#automatic-checks)):

| Observation | Expires after |
| --- | --- |
| Clean, or only upgrade-review findings | 7 days |
| Any other FAIL or WARN | 1 day |
| Unreadable timestamp | immediately |

The clean stamp keeps its own whole-day `> max_age_days` threshold and
version-drift guidance.

### Announcements

Each channel (`session-up` for `booley session up` and `booley session
refresh`, `mcp-tool` for the MCP server) remembers the identity of the last
observation it announced and reports a changed one once. Channels are
independent; `mcp-tool` is `issues_only`. Readers and renderers never write announcement state; only
`consume_changed_snapshot` does.

An `issues_only` channel does not record clean observations. So issue A, then
clean, then the same issue A stays silent on that channel; a channel that
consumes clean observations announces the recurrence.

The Dashboard reads one non-consuming snapshot and renders compact active FAIL
and WARN counts, or `Doctor: OK` when clean. It labels unavailable results and
ends a due result with ` · recheck due`, drawn in the attention color while the
counts keep their severity style. The separate `brief_message()` adapter
retains verbose findings and transcript paths for existing CLI and MCP callers.
