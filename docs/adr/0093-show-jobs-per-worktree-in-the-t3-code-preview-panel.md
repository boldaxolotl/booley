# Show Jobs per worktree in the T3 Code preview panel

Status: accepted (2026-11-13)

Decided in [#3834](https://github.com/boldaxolotl/booley/issues/3834)
(T3 Code as Booley's user interface), after a clickable prototype of three
layouts. Once T3 Code replaces VS Code, this ADR:

- **supersedes [ADR 0068](0068-booley-dashboard.md)** except its Jobs and
  Sandbox resource decisions, which it restates below. The Sessions window,
  session preview and detail, the Goal views, Doctor health, the VS Code
  workspace extension, terminal attach and the unseen-Dashboard protocol go;
- **amends [ADR 0086](0086-launch-flows-and-specialists-from-the-dashboard.md)**:
  the launch form, Standalone Job worker and Specialist forms stand. The
  worktree choice and the Targets window go: the Dashboard's worktree is
  fixed, and Targets are chosen inside the Run form.

## Context

T3 Code is an agent client with its own thread and session list, so the
Dashboard's Sessions window repeats what T3 Code already shows, with less
detail. Jobs, Targets and the Run form have no T3 Code equivalent.

T3 Code offers four places for Booley UI without a fork: the terminal
drawer, the preview panel (a browser view of a page served in the Sandbox),
MCP Apps in a thread, and agent-drawn `html_render`. A permanently visible
Booley panel needs a fork, and #3834 limits the fork to branding. T3 Code
also owns worktrees and threads, and several threads can work in one
worktree.

## Decision

- **One Dashboard per worktree.** The Dashboard shows only Jobs whose
  `work_dir` is its worktree. The main checkout counts as a worktree. There
  is no Sandbox-wide view: Jobs of a deleted worktree appear in no Dashboard.
- **A web page in the preview panel.** One server in the Sandbox serves
  every worktree's Dashboard; the worktree is a URL parameter
  (`?worktree=<path>`). A `t3.json` script opens it. Headless Chrome ships in
  every Sandbox image, because the preview panel needs it and the Dashboard
  is now the only Jobs UI.
- **A feed.** Running and queued Jobs stay on top. Finished Jobs follow as
  result cards in a Results section collapsed by default. A Run button in
  the top bar opens the Run form in place; the form ends on ADR 0086's
  review screen with the equivalent CLI command. The header shows the
  worktree, Sandbox resources and Job slots.
- **Owner is a thread.** A Job records the agent client and thread id that
  started it: T3's thread id for Claude, read from the caller's environment,
  and Codex's own thread id for Codex. The owner shows the T3 thread's name
  when Booley can resolve it, otherwise the client and a short thread id, or
  that a human started it from Run. T3 offers no supported way to map a Codex
  thread id to its T3 thread.
- **Result links per Flow.** Every result card links its report. Each Flow
  declares any further links (logs, coverage, netlists) it wants on the card.
  Waveform opening is out of scope until waveform viewing has a home in T3
  Code.
- **T3 Code's theme.** The page uses T3 Code's default light and dark colors.
- **Optional extra.** A B-Tool may also return an MCP App card for its result
  in the thread. It is not required for the first version.

## Considered options

- **The Textual Dashboard in the terminal drawer.** It already runs there
  unchanged and costs no image size, but a terminal tab is easy to lose, and
  its links still pointed at VS Code. Rejected for a page that matches T3
  Code and can link into it.
- **A Sandbox-wide Dashboard.** It would show Jobs in `/work` and other
  worktrees' Jobs holding shared slots. Rejected: the user works one worktree
  per thread, and the queue position shown on Run covers the slot question.
- **Chrome as an opt-in image variant.** Saves 304 MB, but would make the
  only Jobs UI optional.

## Consequences

- A Job lands in a worktree's Dashboard only when its call was routed there
  (ADR 0082); a Codex call refused for lack of a workspace starts no Job.
- The Session Registry, session-activity hooks and session handles have no
  consumer left and are retired (ADR 0082).
- The glossary's **Booley Dashboard** entry changes when this ships: a web
  page per worktree of Jobs and Run, without sessions, Goals or health.
