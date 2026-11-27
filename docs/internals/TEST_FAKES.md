# Test fakes catalog

This page names the fake that owns each test boundary and the stand-in for each
Booley seam, so a contributor or agent finds it before writing a test. Read it
before you write a fake or patch a command. How to write a good test is
[principle 14](CODING_PRINCIPLES.md#14-tests-prove-behavior) of the coding
principles; this page is for contributors and agents, not users.

The rule: extend the catalogued fake for a boundary instead of adding another.
A new fake module, named with `fake` or `double`, needs a new row for a boundary
or seam that no row covers yet. Copies of a stand-in drift apart, and away from
the tool they imitate.

A stand-in answers in place of an external boundary or a Booley seam: a program
on the search path or a launcher, a patched name, an injected object, or a
canned child process. A boundary is the one command, package, file or name a
fake answers; a seam is a Booley function that a test replaces. Three classes
of support code are not stand-ins and have no row, because they run the real
code:

- Isolation and redirects point real code at a test location or the real tool:
  environment variables, config directories, caches, the test interpreter, the
  built B-Wave binary, and setting a check's declared input such as
  `BOOLEY_CONTAINER=1`.
- Observers and ordering hooks wrap a real function, record or order its calls,
  and call through.
- Real collaborators, data and checks: assertion helpers, budgets, pytest
  plugins, analyzers and their fixture data, drivers of real tools.

`tests/docs/test_fake_catalog.py` fails when a module under `tests/` whose file
name has `fake` or `double` has no row, and when a row links a module that does
not exist. Link each module in a table row with a relative link; the test reads
links from table rows only. The other stand-in modules are listed by behavior
and kept honest by review.

A boundary with several fakes links the issue that merges them.

## Boundaries

| Boundary | Fake | Entry point | Interception | Notes |
|---|---|---|---|---|
| Docker CLI | [tests/fake_docker_executable.py](../../tests/fake_docker_executable.py)<br>[tests/harness/session_docker_fake.py](../../tests/harness/session_docker_fake.py)<br>[tests/harness/sandbox_image_docker_fake.py](../../tests/harness/sandbox_image_docker_fake.py)<br>[tests/harness/sidecar_docker_fake.py](../../tests/harness/sidecar_docker_fake.py)<br>[tests/relay_docker_fake.py](../../tests/relay_docker_fake.py)<br>[tests/session_issuance_support.py](../../tests/session_issuance_support.py)<br>[tests/docker_cli_guard.py](../../tests/docker_cli_guard.py)<br>[tests/docker_host_support.py](../../tests/docker_host_support.py)<br>[tests/path_tool_fake.py](../../tests/path_tool_fake.py)<br>[tests/command_runner_fake.py](../../tests/command_runner_fake.py) | `install_fake_docker`, `FakeDocker`, `FakeDocker.runner`<br>`serve_sandbox_docker`, `route_session_commands`, `install_sandbox_exec`, `serve_migration_docker`<br>`serve_sandbox_image_docker`<br>`serve_egress_docker`<br>`RelayDocker`<br>`serve_host_docker`, `HostDocker`, `stub_keeper`<br>`docker_cli_absent`, `docker_cli_answers`<br>`fake_docker_host`<br>`PathToolFake.install`<br>`CannedRunner` | program on the search path (on Windows a `subprocess.Popen` shim for the bare name); injected runner<br>patch the Session runners; `docker` on the search path for `exec`; patch `booley_dir_migration.run_captured_bounded`, `shutil.which`<br>patch `subprocess.run`, `subprocess.Popen`<br>patch `host_sidecars._docker_adapter`<br>injected runner<br>patch `host_docker._run_docker`, `session_runtime._run`, `project_image.run_captured_bounded`<br>patch `host_docker._run_docker`, `run_captured_bounded`<br>patch `project_image.probe_docker_host_memory`<br>program on the search path (`docker info` in `tests/ci/test_agent_readiness.py`)<br>injected runner (`docker info` and image probes in `tests/audit/`) | `tests/fake_docker_executable.py` is the one fake for this boundary; [#5515](https://github.com/boldaxolotl/booley/issues/5515) moves the Session, Sandbox Image, sidecar and relay fakes onto it |
| Git | [tests/git_command_fake.py](../../tests/git_command_fake.py) | `GitCommandFake.install`, `answer_git_version` | patch `subprocess.run`, `platform_paths.run_captured_bounded` | The Doctor tests' base fake answers Git too: [#5666](https://github.com/boldaxolotl/booley/issues/5666) |
| Live Git process scan | [tests/git_support.py](../../tests/git_support.py) | `no_live_git_processes` | patch `booley.runtime.git._iter_git_process_cmdlines` | `harness/git_support.py::fake_powershell` drives the scan's own tests one level down: not a duplicate |
| `powershell` | [tests/harness/git_support.py](../../tests/harness/git_support.py) | `fake_powershell` | program on the search path | see the live Git process scan row |
| Claude CLI | [tests/claude_cli_fake.py](../../tests/claude_cli_fake.py) | `write_claude_cli`, `replay_sdk_attempts`, `replay_sdk_stream` | program the SDK launches; patch `_claude_backend.query` |  |
| Claude Agent SDK package, when absent | [tests/harness/conftest.py](../../tests/harness/conftest.py) | module-level `sys.modules["claude_agent_sdk"]` stub | `sys.modules` entry | installs when `claude_agent_sdk` is not yet in `sys.modules` as the conftest loads, so imports succeed; the Claude CLI row patches `_claude_backend.query`, the name the backend calls: not a duplicate |
| Codex CLI | [tests/codex_cli_fake.py](../../tests/codex_cli_fake.py) | `install_fake_codex`, `replay_codex_run`, `answer_codex_on_host` | `install_fake_codex` puts a program on the search path and places the test in an issued Sandbox through the Sandbox venue fake; `replay_codex_run` patches `_codex_run_subprocess` and `shutil.which` and places the test on the host through that fake; `answer_codex_on_host` puts a program on the search path through `PathToolFake` |  |
| Model provider API for the installed agent CLIs | [tests/docker/agent_policy_probe.py](../../tests/docker/agent_policy_probe.py) | `_Provider` | local HTTP server injected through `ANTHROPIC_BASE_URL` | in-image probe |
| FPGA Flow's Vivado command | [tests/flows/fpga/vivado_fake.py](../../tests/flows/fpga/vivado_fake.py) | `FakeVivado.install`, `run_fpga` | patch `fpga_edam.fpga_run_command` to a Python child | the child writes a test's metrics as report text; the real report parser reads them |
| Vivado release (provisioning) | [tests/vivado_release_fake.py](../../tests/vivado_release_fake.py) | `fake_vivado_release` | program in the release's `bin` | answers `vivado -version`, not the Flow's run |
| Lint Flow's linter command | [tests/flows/lint/linter_run_fake.py](../../tests/flows/lint/linter_run_fake.py) | `install`, `isolated_verible_fusesoc` | patch `LintFlow._execute`, `fusesoc_registry.resolve_target_handle` |  |
| `fusesoc run --setup` | [tests/fusesoc_test_support.py](../../tests/fusesoc_test_support.py) | `fusesoc_setup_runner` | injected runner | `linter_run_fake` runs the real setup and `target_test_support` calls through to the real `setup_command_for_handle`: not duplicates |
| `iverilog` (failing build) | [tests/flows/failing_compiler.py](../../tests/flows/failing_compiler.py) | `put_failing_iverilog_on_path` | program on the search path |  |
| Simulation adapters' simulator launch (`vvp`, `cocotb-config`, `run_supervised`) | [tests/flows/sim/backends/conftest.py](../../tests/flows/sim/backends/conftest.py) | `fake_launch`, `tool_stub`, `cocotb_config` | patch `booley.flows.sim.backends.{cocotb,icarus,verilator}.run_supervised`; programs on the search path | no `PathToolFake` answers `vvp` or `cocotb-config`: not a duplicate of the generic search-path row |
| A Flow tool's child process | [tests/canned_process_support.py](../../tests/canned_process_support.py) | `canned_command` | a real child that replays a canned result | `harness/process_fake.py` is the in-memory process a patched `Popen` returns, a different answer: not a duplicate |
| GitHub REST API through `gh api` (CI scripts) | [tests/ci/gh_api_fake.py](../../tests/ci/gh_api_fake.py) | `GhApiFake` | injected runner (`main(..., gh=...)`) | `GhApiFake` serves a script with an injected runner. The other `gh` stand-ins are not duplicates: the digest test's `FakeGh` answers GraphQL, and the confidential guard test's `_FakeGh` and `PathToolFake` uses are programs on the search path |
| Commands found on the search path (generic) | [tests/path_tool_fake.py](../../tests/path_tool_fake.py) | `PathToolFake.install` | program on the search path | the Docker CLI row lists its duplicate use |
| Windows `cmd /c mklink /J` | [tests/mklink_fake.py](../../tests/mklink_fake.py) | `install`, `fail` | `cmd` on the search path |  |
| Gateway token command | [tests/gateway_command_support.py](../../tests/gateway_command_support.py) | `write_token_command`, `count_runs` | launcher command string |  |
| Commands a host probe runs | [tests/command_runner_fake.py](../../tests/command_runner_fake.py) | `CannedRunner`, `refusing_runner`, `FixedLookup` | injected runner and lookup |  |
| Process control: the operating-system process a child wraps | [tests/harness/process_fake.py](../../tests/harness/process_fake.py) | `FakeProcess` | returned by a patched `Popen` |  |
| Process liveness, identity and filesystem steps of Job slot stores | [tests/job_slot_steps.py](../../tests/job_slot_steps.py) | `FakeProcessWorld`, `StepScheduler`, `stepped_root`, `stepped_file_lock_wait` | injected into `SlotStore` |  |
| Doctor's host and Sandbox commands | [tests/harness/doctor_host_support.py](../../tests/harness/doctor_host_support.py) | `serve_healthy_doctor_host`, `answer_doctor_commands`, `hide_container_cli`, `raise_child_peak_rss` | patch `subprocess.run`, `host_diagnostics.inspect_host`, `shutil.which`, `developer_probe.resource` | builds on `tests/harness/test_doctor.py::_patch_environment`, a fake in a test file: [#5666](https://github.com/boldaxolotl/booley/issues/5666) |
| MCP server process (launch) | [tests/mcp_server_launch_fake.py](../../tests/mcp_server_launch_fake.py) | `McpServerLaunchFake.installed` | patch `subprocess.Popen` | answers a different command than the MCP server's endpoint child |
| MCP server's endpoint child | [tests/mcp_runner_fake.py](../../tests/mcp_runner_fake.py) | `install`, `application`, `call` | patch `server._run_subprocess` | tests still patch it inline: [#5671](https://github.com/boldaxolotl/booley/issues/5671) |
| Clock of the file-lock wait | [tests/feedback/conftest.py](../../tests/feedback/conftest.py) | `lock_wait_expires` | patch `booley.core.file_lock.monotonic` | each clock row answers a different module's clock name ([principle 16](CODING_PRINCIPLES.md#16-verification-is-deterministic-and-bounded)): not duplicates |
| Deadline timer of the Simulation adapters | [tests/flows/sim/backends/conftest.py](../../tests/flows/sim/backends/conftest.py) | `never_firing_timer`, `instant_timer` | patch `threading.Timer` | see the file-lock clock row |
| Clock and waits of Job slot stores and the Simulation Record lane scheduler | [tests/job_slot_steps.py](../../tests/job_slot_steps.py) | `FakeProcessWorld`, `StepScheduler`, `StepEvent`, `StepCondition`, `stepped_turn` | injected `now`, `sleep` and `renewal_wait`; the lane scheduler's injected `clock`, `event`, `take_turn` and `condition` | see the file-lock clock row |
| Host platform (Windows paths, `sys.platform`, deadline policy) | [tests/host_platform_fake.py](../../tests/host_platform_fake.py) | `use_windows_paths`, `use_sys_platform`, `use_windows_deadline_policy` | patch | environment variables use `monkeypatch.setenv` |
| Windows file-sharing violations | [tests/sharing_error_support.py](../../tests/sharing_error_support.py) | `sharing_error`, `hold_opens`, `hold_os_opens`, `hold_unlinks`, `hold_replaces`, `flaky`, `no_retry_delay` | patch `Path.open`, `os.open`, `Path.unlink`, `Path.replace`, the retry delays |  |
| Which standard modules import | [tests/python_modules.py](../../tests/python_modules.py) | `set_importable` | `sys.modules` entries | different packages and questions than the Claude Agent SDK and `tomlkit` rows |
| Installed `tomlkit` version | [tests/codex_config_support.py](../../tests/codex_config_support.py) | `tomlkit_copy` | returns a module copy that the test puts in `sys.modules` |  |
| Terminal (stdin, colors) | [tests/conftest.py](../../tests/conftest.py)<br>[tests/harness/init_agent_support.py](../../tests/harness/init_agent_support.py) | `_terminal_as_under_capture`, `use_terminal_stdin`<br>`empty_host` | autouse; patch `sys.stdin`, `colors.COLORS_ENABLED`<br>patch `builtins.input` | [#5669](https://github.com/boldaxolotl/booley/issues/5669) |
| Memory Budget computation (`compute_memory_budget`) | [tests/memory_budget_support.py](../../tests/memory_budget_support.py) | `pin_budget`, `fail_budget`, `measure_or_fall_back` | patch `compute_memory_budget` in `memory_admission` and `web_snapshot` | the `tests/conftest.py` fixtures `_memory_budget_is_measurable_everywhere` (autouse) and `pin_memory_budget` only call this module |
| Container OOM event counters (cgroup) | [tests/oom_events_support.py](../../tests/oom_events_support.py) | `rising_oom_events` | patch `job_resources.cgroup_memory_events` |  |
| `/proc` OOM scores and process tree | [tests/oom_priority_support.py](../../tests/oom_priority_support.py) | `FakeProc`, `inert_oom_proc`, `PROC_PATH` | patch `oom_priority.PROC` | reads `/proc`, not the cgroup counters above: not a duplicate |
| Sandbox venue check (host or Sandbox) | [tests/sandbox_venue_fake.py](../../tests/sandbox_venue_fake.py) | `place`, `place_child`, `point_mounts` | `BOOLEY_CONTAINER` (unset, `0` or `1`); patch `runtime_context._MOUNTINFO` to a fixture mount table, `runtime_context._CONTAINER_MARKER_FILES`, `agent._inside_container` and `_codex_backend._inside_container` | `session_runtime`, `enter_sandbox`, `pin_venue`, `install_fake_codex`, `replay_codex_run`, `_issued_sandbox_mounts`, `hand_written_sandbox` and the completed child script of `owned_scratch_support` build on it; test files that still patch the venue inline: [#5737](https://github.com/boldaxolotl/booley/issues/5737) |
| Sandbox image contents Doctor checks | [tests/harness/doctor_run_support.py](../../tests/harness/doctor_run_support.py) | `enter_sandbox` | patch `web_isolation.policy_error`, `activity_hooks.hooks_error`, `dashboard_extension.installed_error` |  |
| Runtime files and version probe of the diagnostics | [tests/diagnostic_helpers.py](../../tests/diagnostic_helpers.py) | private helpers | injected runner; patch `Path.is_dir`, `Path.exists`, `Path.lstat` |  |
| Host named-pipe support (waveform streaming) | [tests/flows/sim/conftest.py](../../tests/flows/sim/conftest.py) | `declared_trace_files_convert_after_the_run` | patch `waveform_store.can_stream_waveform` |  |
| uvicorn's serving loop | [tests/uvicorn_support.py](../../tests/uvicorn_support.py) | `capture_serving` | patch `uvicorn.Server.run` |  |
| Native B-Wave binary (`bwave build`, `bwave list`, queries) | [tests/conftest.py](../../tests/conftest.py)<br>[tests/bwave/native_binary.py](../../tests/bwave/native_binary.py) | `fake_bwave`<br>`fake_native_binary` | launcher; patch `waveform_store.native_bwave_binary`<br>launcher named by `BOOLEY_BWAVE_BIN` that runs a `PathToolFake` (through `/bin/sh` on POSIX) | `fake_bwave` is the `bwave build` stand-in that reads a pipe; [#5754](https://github.com/boldaxolotl/booley/issues/5754) merges them |

## Booley port stand-ins

These replace a Booley function or object (a seam) rather than an external
program. [Principle 14](CODING_PRINCIPLES.md#14-tests-prove-behavior) asks to
patch a Booley name only where it is the boundary, so each row here is a place
to simplify, not to copy.

| Seam | Stand-in | Entry point | Interception | Notes |
|---|---|---|---|---|
| A Flow's prepared invocation | [tests/flows/invocation_doubles.py](../../tests/flows/invocation_doubles.py) | `prepare_as`, `replace_run` | patch |  |
| Project root discovery and config cache (`shared_infra.resolve_project_root`, `_TOML_CACHE`) | [tests/flows/conftest.py](../../tests/flows/conftest.py)<br>[tests/mcp_tools/conftest.py](../../tests/mcp_tools/conftest.py) | `_set_flow_project_root`<br>`_set_booley_dir` | patch `shared_infra.resolve_project_root`<br>patch `shared_infra.resolve_project_root` | [#5664](https://github.com/boldaxolotl/booley/issues/5664) |
| The agent call (`booley.runtime.agent.call_agent`) | [tests/agent_call_fake.py](../../tests/agent_call_fake.py) | `install_agent`, `ScriptedAgent` | patch `booley.runtime.agent.call_agent` | `tests/mcp_tools/reviewer_support.py::install_agent` builds on it to answer per review guide: not a duplicate |
| Agent workspace snapshot (`specialist.isolated_agent_workspace`) | [tests/mcp_tools/reviewer_support.py](../../tests/mcp_tools/reviewer_support.py) | `install_agent` | patch `specialist.isolated_agent_workspace` |  |
| Leased build session (`SimulationBuildSession.capture_inputs`, `reusable_key`, `authorize_fresh_image`) | [tests/flows/sim/leased_session.py](../../tests/flows/sim/leased_session.py) | `canned_build_session`, `split_invoker` | `patch.object` on the three methods |  |
| Target selection (`TargetCatalog.build`) and the setup argv | [tests/target_test_support.py](../../tests/target_test_support.py)<br>[tests/flows/sim/simulation_record_test_support.py](../../tests/flows/sim/simulation_record_test_support.py) | `install_lenient_target_catalog`, `make_target_handle`<br>`_interrupted_simulation_record` | patch `TargetCatalog.build`<br>patch `TargetCatalog.build` | [#5668](https://github.com/boldaxolotl/booley/issues/5668) |
| Simulation execution edge (`SimulateFlow._execute_boundary`, `SimulationExecution._prepare_build`) | [tests/flows/sim/simulation_record_boundary.py](../../tests/flows/sim/simulation_record_boundary.py) | `run_simulation_record_sim` | patch |  |
| Simulation Record executor, execution, sizing, admission endpoint and store | [tests/flows/sim/simulation_record_test_support.py](../../tests/flows/sim/simulation_record_test_support.py) | builders in the module | patch |  |
| Compile surface and Target identity (`resolve_target_compile_surface`, `SimulateFlow._target_identity`) | [tests/flows/sim/conftest.py](../../tests/flows/sim/conftest.py) | fixtures in the module | patch |  |
| Host credential scan (`auth_token.credential_plan`) | [tests/devcontainer_spec_support.py](../../tests/devcontainer_spec_support.py) | `build_spec` | injected `CliSeeds` |  |
| Host Bootstrap and the Sandbox Image chain (`init_cmd.reconcile_bootstrap`, `image_lifecycle.reconcile_planned`) | [tests/harness/init_agent_support.py](../../tests/harness/init_agent_support.py) | `stub_host_images` | patch |  |
| Sandbox owner inventory (`session_runtime.sandbox_attachment`) | [tests/conftest.py](../../tests/conftest.py) | `fake_sandbox_attachment` | patch |  |
| Feature Flag registry (`feature_flags.FEATURE_FLAGS`) | [tests/conftest.py](../../tests/conftest.py) | `feature_flags` | patch |  |
| Host inspection (`host_diagnostics.inspect_host`) | [tests/harness/doctor_host_support.py](../../tests/harness/doctor_host_support.py) | `hide_container_cli` | patch |  |
| Sandbox mount point (`sandbox_layout.BOOLEY_DIR_TARGET`) | [tests/mount_change_support.py](../../tests/mount_change_support.py) | `simulate_mount_change` | patch |  |
| Runtime spec validator prefix (`runtime_spec._validator_prefix_anchors`) | [tests/session_issuance_support.py](../../tests/session_issuance_support.py) | `install_trusted_validator` | patch |  |
