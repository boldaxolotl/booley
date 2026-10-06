# Goal Mode elicitation spike (ADR 0067, Phase 0)

Validated on 06 OCT 2026 for [ADR 0067](../../adr/0067-replace-ticket-mode-with-goal-mode.md).
Decides how `goal_propose_change` asks the human for approval, per client.

## Setup

- picorv32 Project in a Sandbox built from `main` at `07aef4f3f` (Booley
  0.3.0, `mcp` SDK 2.3.0 in the image), real Booley HTTP transport
  (`booley.mcp.server --transport http`, `json_response=True`, loopback).
- Two throwaway tools added to the server for the spike and removed
  afterwards: `spike_input_required` returned `InputRequiredResult` with one
  form request (`decision: approve|reject` enum, `reason: string,
  minLength 1`) and `request_state="spike-proposal-1"`, then echoed the
  retry; `spike_elicit_form` called `ServerSession.elicit_form` mid-call.
- Clients: Claude Code 2.1.285 and Codex 0.160.0, each started inside the
  Sandbox (`booley session enter -- claude|codex`), driven by a human at the
  keyboard. The server appended every call to a JSON-lines log in the
  Project directory; the human reported what the screen showed.

## Results

| | Claude Code 2.1.285 | Codex 0.160.0 |
| --- | --- | --- |
| Negotiated protocol | `2026-07-28` | `2026-07-28` |
| `ClientCapabilities.elicitation` | `form: {}`, `url: {}` | `form: {}`, `url: {}` |
| `input_required` form shown | yes: client renders title, enum selector, text field, Accept/Decline | no: nothing on screen |
| Retry carries `input_responses` | yes: `action: accept`, `content: {decision, reason}` typed by the human | yes: `action: decline`, no content, within the same second |
| Retry carries `request_state` | yes, verbatim | yes, verbatim |
| Decline behaviour | Esc returns `action: cancel`, no content | auto-declines; the human is never asked |
| Agent sees the form | no: the client handles the round trip and retries; the agent only sees the final result | no |
| `elicit_form` mid-call | `NoBackChannelError` | `NoBackChannelError` |

Observed in the Codex transcript after the auto-decline: the agent retried
the tool with `input_responses` and `request_state` copied into the tool
**arguments**, carrying the approval it had been asked to obtain. The wire
retry still said `decline`. Approval is therefore read from the request's
`input_responses` only; anything under `arguments` is ignored.

## Why `elicit_form` cannot work

Both clients choose the 2026-07-28 wire. In `mcp` 2.3.0 that wire is
served by `mcp.server._streamable_http_modern`, whose
`_SingleExchangeDispatchContext.can_send_request` is a field fixed at
`False` (`init=False`) and which constructs every request's
`TransportContext(..., can_send_request=False)`, so a server-initiated
`elicitation/create` has no channel regardless of `json_response` or an
SSE `Accept` header. Read in `mcp` 2.2.0 source; the image ran 2.3.0 and
produced the same `NoBackChannelError`. The
legacy stateful transport does have a back-channel, but neither client uses
it against Booley, so it is not supported.

## Decision for D1

- A client that declares `elicitation.form` gets `InputRequiredResult` from
  `goal_propose_change`. The retry is an elicited decision only when its
  response is `accept` with content; `content.decision` then decides:
  `approve` applies the change, `reject` records a human rejection, and
  both are logged with the typed reason. `decline`, `cancel`, a missing
  response, or content that fails the schema leaves the proposal pending
  and the tool returns `approval_required`, after which the agent-recorded
  path applies (`approval_quote` on a second call, entry marked
  `agent-recorded` in the change log and review package).
- In every exchange tested, Claude Code 2.1.285 produced elicited decisions
  and Codex 0.160.0 auto-declined, so Codex sessions reached the
  agent-recorded path each time. Other versions or configurations were not
  tested; the change log records which response the client gave, so the
  review package never depends on this expectation.
- A client without `elicitation.form` skips the form and goes straight to
  the agent-recorded path.
- `request_state` is sealed with the SDK's `RequestStateBoundary` and bound
  to the proposal id; `input_responses` is read from the request params
  only. Neither authenticates the human: the server cannot tell a form
  answered in the client from a retry forged by any process in the Sandbox
  (the curl probe in this spike supplied a request-level `inputResponses`
  itself). "Elicited" means "answered through the client's form as far as
  Booley can see"; an agent with shell access could forge it, which ADR
  0067 already accepts for a client hook. To make forgery visible, the
  change log records the peer process of the retry (see the
  [session identity note](0067-goal-mode-session-identity.md)) next to the
  session's own, and the review package shows a mismatch.
- No `elicit_form` path, no SSE variant.
