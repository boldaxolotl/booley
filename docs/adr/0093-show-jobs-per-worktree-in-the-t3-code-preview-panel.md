# Show Jobs per worktree in the T3 Code preview panel

Status: accepted (2026-11-13)

Decided in [#3834](https://github.com/boldaxolotl/booley/issues/3834)
(T3 Code as Booley's user interface), after a clickable prototype of three
layouts. Once T3 Code replaces VS Code, this ADR **amends
[ADR 0086](0086-launch-flows-and-specialists-from-the-dashboard.md)**: the
launch form, Standalone Job worker and Specialist forms stand. The
worktree choice and the Targets window go: the Dashboard's worktree is
fixed, and Targets are chosen inside the Run form.

## Context

T3 Code is an agent client with its own thread and session list, so a Booley
view of sessions would repeat what T3 Code already shows, with less detail.
Jobs, Targets and the Run form have no T3 Code equivalent.

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
- **Jobs, not sessions.** The Dashboard has no session list, Goal view or
  Doctor health: T3 Code lists threads, and Doctor reports health.
- **A web page in the preview panel.** One server in the Sandbox serves
  every worktree's Dashboard; the worktree is a URL parameter
  (`?worktree=<path>`). It starts with T3 Code on a fixed loopback port. A
  locked T3 Code button opens it with no worktree, since script URLs are
  literal; the page asks which one unless the main checkout is the only
  choice. Headless Chrome ships in every Sandbox image, because the preview
  panel needs it and the Dashboard is the only Jobs UI.
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

- **A terminal view in the terminal drawer.** It costs no image size, but a
  terminal tab is easy to lose. Rejected for a page that matches T3 Code and
  can link into it.
- **A Sandbox-wide Dashboard.** It would show Jobs in `/work` and other
  worktrees' Jobs holding shared slots. Rejected: the user works one worktree
  per thread, and the queue position shown on Run covers the slot question.
- **Chrome as an opt-in image variant.** Saves 304 MB, but would make the
  only Jobs UI optional.
- **The agent opens it (`preview_open`).** It names the worktree, but only when
  the agent chooses to. It can be added later.

## Consequences

- A Job lands in a worktree's Dashboard only when its call was routed there
  (ADR 0082); a Codex call refused for lack of a workspace starts no Job.
- The Session Registry, session-activity hooks and session handles have no
  consumer left and are retired (ADR 0082).
