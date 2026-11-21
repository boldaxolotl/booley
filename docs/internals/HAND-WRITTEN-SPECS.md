# Hand-written devcontainer specs

Booley normally writes a Project's devcontainer spec with `booley init`. For one
written by hand, such as the Codespace of the
[picorv32 demo](https://github.com/boldaxolotl/booley-prj-picorv32), the spec
must contain the following or the Sandbox will not start.

## Sandbox egress mode (`BOOLEY_EGRESS`)

In `containerEnv`, set `BOOLEY_EGRESS=unproxied` when the container cannot
reach Booley's egress proxy, as in a Codespace. Booley never guesses it from the
platform, and it must match the host `[sandbox] egress` setting
([ADR 0077](../adr/0077-declare-unproxied-sandboxes-explicitly.md) has the modes).

## Agent app in a hand-written spec (`BOOLEY_AGENT_APP`)

In `containerEnv`, set `BOOLEY_AGENT_APP` to `claude`, `codex`, `none`, or
`auto`. Set `BOOLEY_CONTAINER=1` too when the image has neither `/.dockerenv`
nor `/run/.containerenv`. Booley picks the agent app at Sandbox start, so the
spec's `postStartCommand` (and `postCreateCommand`, if any) must run
`python -m booley.harness.incontainer_register`.

## Project setup in a hand-written spec (`booley init`)

Run `booley init` in the spec's `onCreateCommand`. In a hand-written Sandbox it
does Project Initialization only, is idempotent, and refuses the host-only
options; run `booley init` on the host for those.

## Billing policy in a hand-written spec (`[agent] auth`)

Record `auth = "visitor"` in the Project's `booley.toml` (or a `visitor` entry
in `[agent.auth]`) for the visitor mode. Each CLI it covers uses the credential
the person in the Sandbox signs in with or exports, else the gateway the spec
provides.

- The mode is honoured only in a hand-written Sandbox: a container with no
  `BOOLEY_AGENT_CLIS` record whose spec sets `BOOLEY_AGENT_APP` or
  `BOOLEY_PRIMARY_AUTH`. Elsewhere `visitor` bills the subscription,
  `booley doctor` fails on it, and `booley init` on the host replaces it with
  the detected policy.
- A missing `auth` bills the subscription in a hand-written Sandbox too, and
  `booley doctor` fails on it.
- With `BOOLEY_AGENT_APP=auto` and no `BOOLEY_PRIMARY_AUTH`, the start-time
  registrar and the in-container credential setup look up each CLI's
  credential under the policy the Project records (`booley.toml` in
  `BOOLEY_DIR`, else the Project's own Booley Directory). A CLI recorded
  `subscription`, or with no `auth`, is not chosen for an exported API key; the
  gateway is the fallback only for a Codex recorded `visitor`. An unreadable
  `booley.toml` reads as a missing `auth`. A start that no longer picks Codex
  removes Booley's gateway provider from Codex's config. A
  `BOOLEY_PRIMARY_AUTH` hand-off still overrides the recorded policy.
- `booley init` in the spec's `onCreateCommand` keeps a recorded `visitor`.
  `--auth` and the prompt do not offer it.
- `BOOLEY_PRIMARY_AUTH` in the spec still sets the default CLI's policy.

Why: [ADR 0094](../adr/0094-record-the-visitor-mode-explicitly.md).
