# Route Codex Specialists through an env-selected gateway

Status: accepted

## Context

An operator may run a gateway that fronts and bills the model service. Codex
Specialists must run against it with no native OpenAI credential, login or
`auth.json` in the Sandbox. The spike
([#2264](https://github.com/boldaxolotl/booley/issues/2264)) verified that a
Codex custom provider with an `auth` command sends the command's output as the
bearer token, and that the command inherits Codex's environment. This is the
first slice, [#2266](https://github.com/boldaxolotl/booley/issues/2266);
#2267 (token command), #2268 (model pin), #2269 (text-only Codex) and #2270
(interactive registrar) follow.

## Decision

`BOOLEY_PRIMARY_AUTH=gateway` is a fourth auth policy, selected only by the
environment with `BOOLEY_GATEWAY_URL` (an `https://` base URL, passed to Codex
verbatim) and `BOOLEY_GATEWAY_TOKEN`.

- **Environment only.** `[agent] auth` keeps rejecting `gateway`: URL and token
  have no Project home.
- **Honored only inside a container.** Booley-issued Sandboxes take their
  policy from `booley.toml` and never receive `BOOLEY_GATEWAY_*`, so a host
  export is ignored; Doctor WARNs `agent.credential-policy` and `booley auth
  --status` says so. `env_auth_handoff()` is the one normalizing reader.
- **Codex only.** A Claude provider fails in Doctor and `require_credential()`.
- **Token delivery.** The private Codex home's `config.toml` selects a
  `booley_gateway` provider (`wire_api = "responses"`) whose
  `[model_providers.booley_gateway.auth]` runs
  `python -P -m booley.runtime.gateway_token`. The helper prints the token from
  the environment Codex inherited, so it never reaches disk or a log; `-P`
  keeps a Project `booley/` directory off the import path. `fast_mode` is off
  because gateways reject the priority tier. This command is the extension
  point for #2267.
- **No native credential.** No credential variable survives into the Codex
  child, no `auth.json` is copied, and `BOOLEY_GATEWAY_TOKEN` is excluded from
  the generated MCP server environment.
- **`ANTHROPIC_AUTH_TOKEN` is tracked, scrub-only.** Policies that disallow it
  (`subscription`, `api_key`, `gateway`) remove it from agent environments.
  Booley never forwards it into a Sandbox spec; a hand-written spec may carry
  it only as `${localEnv:ANTHROPIC_AUTH_TOKEN}` under `auto`.
- **Fail loud.** A missing or non-`https` URL, userinfo or whitespace in the
  URL, or a missing token fails before any spawn, naming the variable without
  echoing the value. Text-only Codex Specialists (#2269) fail naming the policy.
- **Registrar.** The in-Sandbox registrar returns early under `gateway`;
  gateway registration is #2270.

## Rejected alternatives

**`env_key` provider auth** exports the token to every tool Codex launches; the
`auth` command confines it to one short-lived helper and lets #2267 add refresh.
**A `booley.toml` knob** puts a secret's location in a committed file.
**`-c` overrides** expose the token in process arguments.
**Honoring the policy on the host** would silently change what a host run bills.
