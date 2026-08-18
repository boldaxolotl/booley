# AGENTS.md Minimal Template

Use this structure for a concise Project-level `AGENTS.md`. Keep only facts that
change RTL work; omit unknown or low-value details.

This content is written to the canonical `<project_dir>/AGENTS.md`; the RTL repo
root only gets generated links to it (see `steps/3-agents-md.md`, Step 3).

```markdown
# AGENTS.md

## Project-Specific Instructions

- Project purpose: <one sentence describing what this Project builds.>
- Source ownership: <which paths are primary source, derived source, tests, specs, read-only dependencies, or submodules.>
- Project gotchas: <human-provided rules a future assistant would otherwise miss. Do not infer gotchas automatically.>

## Booley-Specific Instructions

- Project-specific Booley data lives in `.booley_project/`.
- Keep `booley doctor` green: no active warnings or errors. After changing
  project configuration, EDA-tool Targets, dependencies, or the Session Runtime,
  fix every finding or add a narrow, reviewed `doctor-waivers.toml` entry for a
  deliberate constraint. Run plain Doctor on the host and plain plus `--deep`
  in the Session Runtime before handoff; never leave an active warning for
  later.
- Write handoff documents and plans under `.booley_project/` (e.g. `.booley_project/plans/`, `.booley_project/handoffs/`), never in the RTL repo tree. They are working notes for Booley agents, not project source — keeping them out of the main repo avoids polluting the source tree and its git history.
- In Interactive Mode, you are working through the editor extension in the user's VS Code window, attached to this Project's Session Runtime (devcontainer). Use that shared VS Code window as the human-facing surface: when useful, open files with `code --goto <path>[:<line>]` and side-by-side diffs with `code --diff <left> <right>` instead of only printing their paths.
- The Booley endpoints below exist **only inside the Session Runtime** (this Project's devcontainer). They are **MCP tools, not CLI programs**: check for them in your own MCP tool list, never with `command -v booley_status` / `which` / `booley_status --help`. A `$PATH` probe always comes back empty and will make you wrongly conclude the MCP tools are missing. If your harness hides MCP tools behind a code-mode sandbox, the list is `ALL_TOOLS` — search it for `mcp__booley__booley_status`.
- If, and only if, `booley_status` is genuinely absent from that MCP tool list, you are running on the host: the MCP tools were never registered here, and nothing is broken. Say so, and point the user at "Reopen in Container" (or `booley session up && booley session enter`). Do not work around it with raw EDA commands.
- At the start of an Interactive Mode tab, call `booley_status` and display its returned status block.
- RISC-V reference docs (keep only when `[sandbox].image = "booley-sandbox-riscv"`): offline ISA manual HTML/PDF, debug specification, and ELF psABI live at `$BOOLEY_RISCV_DOCS` (`/opt/riscv-docs`). Start with `$BOOLEY_RISCV_DOCS/INDEX.md`; search the HTML directly or extract a PDF with `pdftotext <file.pdf> -`.
- Use the Booley Flows enabled in this project's `booley.toml` for RTL feedback — typically some of `sim`, `lint`, `elab`, `synth`, and `fpga`. MCP tools are discovered automatically; `[flows.<name>].enabled = false` opts a Flow out. Run `booley_status` (or `booley targets`) to see the exact set wired up here.
- Use Booley Specialists for deeper RTL work: `reviewer` and `mutation_tester` when requested or useful.
- Do not invent raw simulator, lint, synthesis, or analysis commands. Use the registered Booley MCP tools and Flows for supported work. If the user explicitly requests a one-off toolchain command with no matching Flow, run it only from the Session Runtime and state that it is outside Booley's normal run-report contract; never run it on the host.

## Working on Another Branch or Commit (git worktrees)

- To build or simulate a different branch/commit without touching the workspace, do NOT run a plain `git worktree add` to an arbitrary path, and never copy or symlink `.booley_project/` by hand — a bare checkout has no Booley state, and the live state dir contains machine-specific runtime data.
- Create worktrees with Booley's helper, which lands them in `.booley_project/worktrees/<name>` and copies a clean `.booley_project` snapshot inside:
  `echo '{"name":"<name>","cwd":"'"$PWD"'"}' | bash "$(python -c 'import booley.dev_support, pathlib; print(pathlib.Path(booley.dev_support.__file__).parent / "worktree_create.sh")')"`
  (a `<branch>--<description>` name checks out `<branch>`; otherwise the worktree is a detached HEAD at the current commit).
- Then pass `work_dir=<worktree path>` to any Booley Flow (sim`, `lint`, `synth`, ...) to run it against that checkout. Omit `work_dir` to run against the normal workspace. Project config still comes from the canonical project dir either way.
- To compare synthesis/implementation QoR against a past commit, prefer the built-in `--baseline <git ref>` argument of `synth`/`fpga` over a manual worktree.
```
