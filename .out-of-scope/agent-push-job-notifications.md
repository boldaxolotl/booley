# Push notifications to the agent when a Job finishes

Booley does not push "Job finished" messages into the agent's conversation.
The agent learns about finished Jobs by polling: `booley_poll` without a
`run_id` waits for the first finished Job the agent owns, and every Booley
MCP tool response lists the agent's finished Jobs it has not read yet
(#2471).

## Why this is out of scope

MCP is request/response. A server notification cannot start a model turn,
so push needs support from whatever runs the agent loop, and each runner
needs its own mechanism:

- a Claude Code `PostToolUse` hook, which is Claude-only and fires only on
  the agent's next tool call
- a "Job finished" message that Booley injects when it drives the loop
  (Goal Mode, Ticket Mode), which does nothing for Interactive Mode
- something else again for Codex

Polling all owned Jobs at once already removes the reported failure: a
finished result sitting unread while the agent blocks on another Job. The
notice on every Booley tool response covers an agent that is busy with other
Booley work. Push would add only three things:

- notices during long stretches with no Booley calls, while the agent is
  doing other useful work anyway
- fewer poll round trips during very long Jobs, which costs little
- waking an agent that ended its turn with Jobs still running, which an
  argument-free poll makes unnecessary

None of that justifies maintaining a separate integration per agent runner.

## When to reconsider

Each Job records when it ended and when its result was first read (#2471).
Reopen this only if that unread latency shows finished results still going
unread for long, for example agents ending sessions with Jobs running, or
long gaps during non-Booley work.

## Prior requests

- #2461: "Notify the agent when a job finishes instead of polling"
