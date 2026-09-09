# Config / Runtime dependency separation — #444

Implementation evidence for [#444](https://github.com/boldaxolotl/booley/issues/444)
and the stabilization observations tracked in
[#279](https://github.com/boldaxolotl/booley/issues/279).

## Stabilization classification

This is an **independent legitimate design change** during #279 stabilization,
not the optional evidence-gated PR 3. Its primary outcome is that Config returns
validated values while Runtime and Project Initialization own live mechanisms.
The Config-to-Runtime prohibition ratchets that intended design after the
separation; it is not the reason for introducing the separation.

This work does not start or waive PR 3, broaden an SCC baseline, or introduce an
abstraction solely to improve graph metrics.

## Baseline

Issue baseline and implementation start: `8f1820dbd6a107f0fff988497cd71516861f1e7c`.
The after state was measured at the reviewed implementation revision
`516149b36c929b4d8f51ab12c8a6c7e6ef3caf38`. Both were captured 09 SEP 2026.

| Diagnostic | Before | After |
| --- | ---: | ---: |
| Parsed Python modules | 445 | 451 |
| Located normalized dependency facts | 2,182 | 2,188 |
| Unique normalized module edges | 1,764 | 1,775 |
| Config → Runtime normalized module edges | 10 | 0 |
| Direct mutual top-level package pairs | 18 | 17 |
| Members in the legacy cyclic package group | 18 | 18 |

The ten baseline edges are:

- `config.agent → runtime.agent_backend`
- `config.agent → runtime.job_slots`
- `config.agent → runtime.project_image`
- `config.guidance_links → runtime.git`
- `config.guidance_links → runtime.init_plan`
- `config.guidance_links → runtime.project_dir`
- `config.host_config → runtime.auth_token`
- `config.project_config → runtime.checkout_role`
- `config.project_config → runtime.project_dir`
- `config.settings → runtime._retry`

The legacy cyclic package group did not broaden. The Config/Runtime mutual pair
disappeared because Runtime still consumes Config values but Config no longer
imports Runtime mechanisms.

## Exact replacement edges

The analyzer reports the following normalized production replacements for each
removed edge (all names are below `booley`):

| Removed edge | Exact replacement edge(s) |
| --- | --- |
| `config.agent → runtime.agent_backend` | `runtime.agent_config → config.agent`; `runtime.agent_config → runtime.agent_backend` |
| `config.agent → runtime.job_slots` | `config.agent → config.jobs`; `runtime.job_slots → config.jobs` |
| `config.agent → runtime.project_image` | `config.agent → config.sandbox`; `runtime.project_image → config.sandbox` |
| `config.guidance_links → runtime.git` | `harness.setup.guidance_links → runtime.git` |
| `config.guidance_links → runtime.init_plan` | `harness.setup.guidance_links → runtime.init_plan` |
| `config.guidance_links → runtime.project_dir` | `harness.setup.guidance_links → runtime.project_dir` |
| `config.host_config → runtime.auth_token` | `config.host_config → core.user_paths`; `runtime.auth_token → core.user_paths` |
| `config.project_config → runtime.checkout_role` | `config.project_config → core.checkout_role`; `runtime.checkout_role → core.checkout_role` |
| `config.project_config → runtime.project_dir` | `config.project_config → core.project_dir`; `runtime.project_dir → core.project_dir` |
| `config.settings → runtime._retry` | No production replacement edge: the facade was removed and its test consumers import `runtime._retry` directly. |

## Ownership and interface migrations

- `booley.config.agent.AgentSettings` is the immutable value returned by Config.
  `booley.runtime.agent_config` is the composition adapter that constructs and
  caches live Claude/Codex backends from those settings.
- Live `BackendConfig`, `get_backend_config`, `load_backend_config`, and
  `set_backend_config` consumers move to `booley.runtime.agent_config`.
- Job-cap parsing and `SlotCaps` move to `booley.config.jobs`.
  `booley.runtime.job_slots` retains static re-exports for compatibility.
- Sandbox image selection and deterministic naming move to
  `booley.config.sandbox`; Runtime retains static image-operation compatibility
  entry points.
- Project-directory and source-checkout classification primitives move to
  `booley.core`, below both Config and Runtime. Runtime paths remain static
  compatibility interfaces and continue to own transient directory creation.
- User config-directory resolution moves to `booley.core.user_paths`, with the
  former Runtime import remaining compatible.
- Guidance-link reconciliation moves from Config to Project Initialization at
  `booley.harness.setup.guidance_links`.
- Retry constants are no longer exported from `booley.config.settings`; callers
  import the Runtime retry policy directly.

The backend cache compares the identity of its paired settings value with the
current Config value. A direct Config reload therefore rebuilds non-injected
Runtime composition, while explicit test injection remains stable.

## Diagnostic-only hotspot measurements

These values do not gate the change:

| Named hotspot | Before | After |
| --- | ---: | ---: |
| `booley.harness.doctor` | 64 | 65 |
| `booley.harness.booley` | 53 | 54 |
| `booley.harness.init_cmd` | 42 | 42 |
| `booley.harness.developer` | 43 | 44 |
| `booley.flows.sim.flow` | 39 | 39 |
| `booley.flows.synth.flow` | 35 | 35 |
| `booley.mcp.server` | 30 | 30 |
| `booley.flows.fpga.flow` | 31 | 31 |
| `booley.specialists.mutation_tester` | 25 | 25 |
| `booley.specialists.coverage_analyst` | 25 | 25 |

The one-edge increases are explicit composition imports replacing ownership
that previously leaked into Config; they are not new cyclic dependencies.

## Verification

- The architecture report identifies zero normalized `booley.config →
  booley.runtime` edges.
- D17 forbids the direction with no waiver or composition exception. Seed tests
  prove that ordinary, function-local, and `TYPE_CHECKING` imports are caught.
- `pytest -q tests/architecture`: 91 passed.
- Focused review-regression and architecture selection: 862 passed.
- `pyright`: 0 errors, 0 warnings.
- `ruff check src/ tests/`: passed.
- `ruff format --check .`: passed.
- Complete test suite: 10,667 passed, 56 skipped.
