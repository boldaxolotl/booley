# AGENTS.md Minimal Template

Use this concise Project-level `AGENTS.md` structure. Keep only facts that change
RTL work; omit unknown or low-value details.

Write it to canonical `<project_dir>/AGENTS.md`; the RTL repo root normally gets
generated links. An explicitly selected hybrid port/integration footprint instead
tracks a content-identical root `AGENTS.md` (see `steps/3-agents-md.md`, Step 3).

```markdown
# AGENTS.md

## Project-Specific Instructions

- Project purpose: <one sentence describing what this Project builds.>
- Source ownership: <which paths are primary source, derived source, tests, specs, read-only dependencies, or submodules.>
- Project gotchas: <human-provided rules a future assistant would otherwise miss. Do not infer gotchas automatically.>

## Booley-Specific Instructions

- Project-specific Booley data lives in `.booley_project/`.
- This repo is operating in stealth mode. No changes to `.booley_project/` may be visible in the main repo. (Keep only for `[stealth].enabled = true`.)
- Keep `booley doctor` green. After changing project configuration, EDA-tool Targets, dependencies, or the Session Runtime, fix every finding or add a narrow, reviewed `doctor-waivers.toml` entry for a deliberate constraint. Before handoff, run plain Doctor on the host and plain plus `--deep` in the Session Runtime; leave no active warnings or errors.
- Keep handoffs and plans under `.booley_project/` (e.g. `.booley_project/plans/`, `.booley_project/handoffs/`), never in the RTL repo. They are agent working notes, not project source, and do not belong in source history.
- In Interactive Mode, the editor extension uses the user's shared VS Code window attached to this Project's Session Runtime (devcontainer). Use it as the human-facing surface: when useful, open files with `code --goto <path>[:<line>]` and side-by-side diffs with `code --diff <left> <right>` instead of only printing paths.
- The Booley endpoints below exist **only inside the Session Runtime**. They are **MCP tools, not CLI programs**: inspect your MCP tool list; never run `command -v booley_status`, `which booley_status`, or `booley_status --help`. `$PATH` probes always fail and falsely imply the tools are missing. If a code-mode sandbox hides tools, search `ALL_TOOLS` for `mcp__booley__booley_status`.
- Only if `booley_status` is absent from that MCP tool list are you on the host, where the tools were never registered and nothing is broken. Point the user to "Reopen in Container" (or `booley session up && booley session enter`); do not substitute raw EDA commands.
- At the start of an Interactive Mode tab, call `booley_status` and display its returned status block.
- RISC-V reference docs (keep only for `[sandbox].image = "booley-sandbox-riscv"`) live at `$BOOLEY_RISCV_DOCS` (`/opt/riscv-docs`): offline ISA HTML/PDF, debug specification, and ELF psABI. Start with `$BOOLEY_RISCV_DOCS/INDEX.md`; search HTML directly or extract PDFs with `pdftotext <file.pdf> -`.
- Use the Booley Flows enabled in this project's `booley.toml` for RTL feedback—typically `sim`, `lint`, `elab`, `synth`, or `fpga`. MCP tools are auto-discovered; `[flows.<name>].enabled = false` opts out. Run `booley_status` (or `booley targets`) for the exact wiring.
- Use Booley Specialists for deeper RTL work: `reviewer` and `mutation_tester` when requested or useful.
- Do not invent raw simulator, lint, synthesis, or analysis commands; use registered Booley MCP tools and Flows. If the user explicitly requests a one-off command with no matching Flow, run it only in the Session Runtime and identify it as outside Booley's run-report contract; never run it on the host.

## Working on Another Branch or Commit (git worktrees)

- To build or simulate another branch/commit without touching the workspace, do not run plain `git worktree add`, use an arbitrary path, or manually copy/symlink `.booley_project/`. A bare checkout lacks Booley state; the live state contains machine-specific runtime data.
- The worktree helper is Session Runtime package data at `booley/dev_support/worktree_create.sh`. Invoke it to create `.booley_project/worktrees/<name>` with a clean `.booley_project` snapshot:
  `echo '{"name":"<name>","cwd":"'"$PWD"'"}' | bash "$(python -c 'import booley.dev_support, pathlib; print(pathlib.Path(booley.dev_support.__file__).parent / "worktree_create.sh")')"`
  (a `<branch>--<description>` name checks out `<branch>`; otherwise the worktree is a detached HEAD at the current commit).
- Pass `work_dir=<worktree path>` to a Flow (`sim`, `lint`, `synth`, ...) to use that checkout; omit it for the normal workspace. Either way, project config comes from the canonical project dir.
- For synthesis/implementation QoR against a past commit, prefer `synth`/`fpga`'s built-in `--baseline <git ref>` over a manual worktree.
```
