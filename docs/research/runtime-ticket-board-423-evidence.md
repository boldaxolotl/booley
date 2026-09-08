# Runtime / Ticket Board dependency separation — #423

Implementation evidence for [#423](https://github.com/boldaxolotl/booley/issues/423)
and the stabilization observations tracked in
[#279](https://github.com/boldaxolotl/booley/issues/279).

Baseline: `84b251527b7ff803a99f4806d7a799a6cb69f2bc`. The after measurement is the
implementation tree on `codex/issue-423-runtime-ticket-board`; the PR identifies
its final commit. Captured 08 SEP 2026.

## Dependency result

| Diagnostic | Before | After |
| --- | ---: | ---: |
| Parsed Python modules | 421 | 422 |
| Located normalized dependency facts | 2,077 | 2,093 |
| Unique normalized module edges | 1,671 | 1,679 |
| Runtime → Ticket Board normalized module edges | 4 | 0 |
| Direct mutual top-level package pairs | 21 | 20 |
| Members in the legacy cyclic package group | 18 | 18 |

The removed normalized edges are:

- `runtime._claude_backend → ticket_board.notifications`
- `runtime.job_records → ticket_board.paths`
- `runtime.prompt_artifacts → ticket_board.paths`
- `runtime.ticket_repositories → ticket_board.workspace_ops`

The last edge had both a type-checking import and a function-local authoring
import at the baseline. Both are removed. The `runtime ↔ ticket_board` mutual
pair is absent; no mutual pair was added, the package SCC member sets are
unchanged, and the Runtime-to-Harness edge set is unchanged. The existing exact
Runtime entry-point permission remains intact. No legacy cycle baseline or
waiver was broadened.

D14 is a **legitimate design change** during #279 stabilization: a new,
unconditional Runtime-to-Ticket Board direction rule. Three isolated source
seeds prove rejection of ordinary, function-local and `TYPE_CHECKING` imports.
The production source passes. This change does not claim the large SCC splits
and does not start the evidence-gated optional PR 3 programme.

## Named hotspot observations for #279

| Named composition hotspot | Before | After |
| --- | ---: | ---: |
| `harness.doctor` | 64 | 64 |
| `harness.booley` | 52 | 52 |
| `harness.init_cmd` | 42 | 42 |
| `harness.developer` | 41 | 42 |
| `flows.sim.flow` | 37 | 37 |
| `flows.synth.flow` | 34 | 34 |
| `mcp.server` | 29 | 29 |
| `flows.fpga.flow` | 29 | 29 |
| `specialists.mutation_tester` | 25 | 25 |
| `specialists.coverage_analyst` | 25 | 25 |

Developer now explicitly composes artifact destinations and notification policy.
Its repository dependency moved to the Ticket Board owner. This is intentional
composition knowledge; fan-out remains diagnostic, with no numeric gate.

## Behavior and validation

Record JSON, prompt hashes/text, transcript rendering, path precedence and
fallback names remain compatible. A per-attempt path resolver receives the
backend's final labeled transcript so retries cannot accidentally reuse a
previous attempt's destinations. Per-call notification injection avoids shared
backend state; preferences are checked at delivery time. Failed notification
delivery cannot abort rate-limit wait/retry or budget cleanup.

Repository discovery and composite Project submodule materialization remain
reusable Runtime mechanics. Ticket Workspace handoff, Scope routing, Board-change
protection and retirement live in Ticket Board. The project materializer retains
its separate outer/inner rollback boundaries. The authoring forwarding method
and unused `project_git_ops` compatibility module are removed.

Completed local validation (overlapping runs; counts are not additive):

- Main focused run: **1,681 passed, 1 skipped**, covering architecture, Runtime,
  Claude/Codex agents, prompts, MCP dispatch/run reports and Ticket Board.
- Additional caller integration run: **313 passed, 3 skipped**, covering Harness
  setup/developer/blocked preparation, review, Specialists, baseline worktrees,
  and late Interactive job-root setup. The three image-smoke tests require the
  production image, EDA toolchain and `/opt/pdk` mount, which are unavailable here.
- Final artifact/rate-limit/job tests: **166 passed**, including public Claude and
  Codex calls, callback delivery failure, retry paths, budget cancellation cleanup,
  isolated Runtime imports/artifact writes, both-mode path precedence and restarted
  polling after late Interactive logging setup.
- Architecture plus explicit execution-port/submodule regressions: **120 passed**,
  including second-selection failure preserving completed outer materialization.
- `ruff check src/ tests/`, `ruff check .`, `ruff format --check .`, and
  `git diff --check`: passed.
- Pyright **1.1.411**, using the configured include/strict scope: **0 errors,
  0 warnings, 0 informations**.

Reproduce the dependency diagnostics from either checkout:

```sh
python3 tests/architecture/report.py --source-root src/booley --top 30
pytest -q tests/architecture/
ruff check src/ tests/
```

The implementation PR references #279 and carries this evidence for its
stabilization record. No independent issue comment is sent by this change.
