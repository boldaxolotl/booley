# AGENTS.md Minimal Template

Use this concise Project-level `AGENTS.md` structure. Keep only facts that change
RTL work; omit unknown or low-value details.

Write it to canonical `<project_dir>/AGENTS.md`; the RTL repo root normally gets
generated links. An explicitly selected hybrid port/integration footprint instead
tracks a content-identical root `AGENTS.md` (see `steps/3-agents-md.md`, Step 3).

The stealth, RISC-V, and worktree bullets are conditional; `steps/3-agents-md.md`
says when to keep each.

```markdown
# AGENTS.md

## Project-Specific Instructions

- Project purpose: <one sentence describing what this Project builds.>
- Source ownership: <which paths are primary source, derived source, tests, specs, read-only dependencies, or submodules.>
- Project gotchas: <human-provided rules a future assistant would otherwise miss.>

## Booley-Specific Instructions

- Project-specific Booley data lives in `.booley_project/`. Keep handoffs and plans there too (`.booley_project/plans/`, `.booley_project/handoffs/`), never in the RTL repo: they are working notes, not project source.
- Stealth mode: `.booley_project/` is a separate Git repository. Commit its contents there; none of them may be visible in the main repo.
- Inside the Sandbox, use the registered Booley MCP tools for EDA work. Flows this Project disables are not registered (the usual set is `sim`, `lint`, `synth`, and `fpga`; `sim --mode elab-only` is an Elaboration Check); call `booley_status` or `booley targets` for exact wiring. Find the tools in your MCP tool list, not through `$PATH` or `--help`; if code mode hides them, search `ALL_TOOLS` for `mcp__booley__booley_status`.
- If `booley_status` is absent from the MCP tool list, you are on the host and nothing is broken. Point the user to "Reopen in Container" (or `booley session up && booley session enter`); do not substitute raw EDA commands.
- Interactive Mode: at the start of a tab, call `booley_status` and display its status block. The user shares a VS Code window attached to the Sandbox, so show files with `code --goto <path>[:<line>]` and diffs with `code --diff <left> <right>` when useful.
- Doctor during task work: verify changes with the relevant Booley Flows and, at most, plain `booley doctor`. `booley doctor --deep` belongs to Project Setup, `/booley-heal`, and Booley version changes; do not run it for task work or handoffs. Report findings your change did not cause (pre-existing, host-owned, or Sandbox provisioning) in the handoff instead of fixing them. Never edit `booley.toml`, `.core` files, `doctor-waivers.toml`, or Goal Records to silence such a finding. If the task itself changes project configuration, Targets, dependencies, or the Sandbox, fix the findings it caused or add a narrow, reviewed waiver for a deliberate constraint, and state that deep verification (`/booley-heal`) is due.
- RISC-V reference docs (ISA, debug specification, ELF psABI) live at `$BOOLEY_RISCV_DOCS`; start with `INDEX.md` there.
- Booley Specialists for deeper RTL work: `coverage_analyst`, `reviewer`, `mutation_tester`.
- Another branch or commit: never use plain `git worktree add` or copy `.booley_project/`. Commit Project changes, then run `booley worktree new <name>` inside the Sandbox from the workspace root (`--help` explains naming). Pass `work_dir=<worktree path>` to a Flow to use that checkout; project config still comes from the canonical project dir. To remove a worktree, follow the steps the command printed; when it printed none, run `git worktree remove <worktree path>`. For QoR against a past commit, prefer `synth`/`fpga` `--baseline <git ref>`.
```
