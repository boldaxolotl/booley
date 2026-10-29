# Route Codex Specialists through an env-selected gateway

Status: accepted (amended 2026-10-29: token command, see Amendments)

## Context

An operator may run a gateway that fronts and bills the model service. Codex
Specialists must run against it with no native OpenAI credential, login or
`auth.json` in the Sandbox. The spike
([#2264](https://github.com/boldaxolotl/booley/issues/2264)) verified that a
Codex custom provider with an `auth` command sends the command's output as the
bearer token, and that the command inherits Codex's environment. This is the
first slice, [#2266](https://github.com/boldaxolotl/booley/issues/2266); #2267
to #2270 follow.

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
  the environment Codex inherited, so it never reaches a config file; the
  generated `[shell_environment_policy]` excludes it from the tools Codex runs
  (Codex 0.160 ignores its default `*TOKEN*` filter); `-P` keeps a Project
  `booley/` directory off the import path. `fast_mode` is off
  because gateways reject the priority tier. The token may also come from
  `BOOLEY_GATEWAY_TOKEN_COMMAND` (amended 2026-10-29, see Amendments).
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
- **Registrar.** The in-Sandbox registrar returns early (#2270).

## Rejected alternatives

**`env_key` provider auth** has no refresh path; the `auth` command keeps the
token out of the config file and lets a refreshed token reach Codex.
**A `booley.toml` knob** puts a secret's location in a committed file.
**`-c` overrides** expose the token in process arguments.
**Honoring the policy on the host** would silently change what a host run bills.

## Amendments (2026-10-29)

[#2267](https://github.com/boldaxolotl/booley/issues/2267) adds a second token
source, `BOOLEY_GATEWAY_TOKEN_COMMAND`. Set exactly one of it and
`BOOLEY_GATEWAY_TOKEN`; both or neither fails loud.

- **The command runs in the Booley process that spawns Codex**, not inside
  Codex's `auth.command`. A helper process has no memory, so it cannot keep the
  5-minute in-memory cache or retry a run after a rejected token, and Codex runs
  its auth command about three times per launch (#2264), which would multiply
  the runs of a command that may claim a token.
- **Delivery is unchanged.** Booley puts the result in the Codex child's
  environment as `BOOLEY_GATEWAY_TOKEN` and removes the command variable, so
  the unchanged helper prints it, `[shell_environment_policy]` still hides it
  from Codex tools, and neither variable reaches `config.toml`, the nested MCP
  server environment or a process argument.
- **Refresh.** An HTTP 401 from the gateway (`AgentAuthError`) drops the cached
  token, re-runs the command and retries the run once. A second rejection
  propagates. A token that expires during a run is out of scope.
- **Contract and threat model.** The command prints the current token and is
  safe to run repeatedly (Doctor runs it too). Its stderr is shown verbatim on
  failure; its stdout never is. Whoever can run the command in the Sandbox can
  mint a token.
