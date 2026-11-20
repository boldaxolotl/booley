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

For the visitor mode, the Project's `booley.toml` must record `auth = "visitor"`
(or a `visitor` entry in `[agent.auth]`). Without it a CLI bills the
subscription and `booley doctor` fails on the missing `auth`. `booley init`
in the spec's `onCreateCommand` keeps a recorded `visitor`. `BOOLEY_PRIMARY_AUTH`
in the spec still sets the default CLI's policy. See
[CONFIG.md](../user/CONFIG.md#pinning-what-bills-agent-auth).
