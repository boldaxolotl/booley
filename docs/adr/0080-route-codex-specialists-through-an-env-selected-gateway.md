# Route Codex Specialists through an env-selected gateway

Status: accepted

## Context

An operator may run a gateway that fronts and bills the model service. Codex
must run against it with no native OpenAI credential, login or `auth.json` in
the Sandbox. A Codex custom provider with an `auth` command sends the
command's output as the bearer token, and the command inherits Codex's
environment.

## Decision

### Selection and scope

- **The gateway is a per-CLI auth policy, and only Codex may be on it.** The
  gateway routes Codex, never Claude. `[agent.auth] codex = "gateway"` selects
  it in a Booley-issued Sandbox; `claude = "gateway"` is malformed. A
  hand-written Sandbox selects it for Codex with `BOOLEY_PRIMARY_AUTH=gateway`.
  The per-CLI record is authoritative, so interactive Codex uses the gateway
  whichever CLI is the default.
- **The endpoint and token have no Project home.** `BOOLEY_GATEWAY_URL` and
  one token source come only from the environment. A `booley.toml` knob would
  put a secret's location in a committed file.
- **The policy is honored only inside a Sandbox.** A host export would
  silently change what a host run bills, so the host ignores it and Doctor
  warns. A Booley-issued Sandbox forwards the URL and the one token source as
  `${localEnv:...}` references, never values or a model pin; validation
  refuses anything else, and a Codex login seed under the gateway.
- **No native credential survives.** No credential variable reaches the Codex
  child, no `auth.json` is copied, and the token stays out of the generated MCP
  server environment.
- **Fail loud, before any spawn.** A missing or non-`https` URL, userinfo or
  whitespace in it, or a missing token fails naming the variable, never
  echoing a value.

### Token delivery

- **The token never reaches a config file or a process argument.** The Codex
  config selects a Booley provider whose `auth` command is a helper that prints
  the token from the environment Codex inherited. `-c` overrides would expose
  it in process arguments; `env_key` provider auth has no refresh path.
  The helper runs without the Project on its import path.
- **The token is hidden from the tools Codex runs.** The generated config
  excludes it explicitly; Codex's default secret-name filter is not relied on.
- **The priority tier is off.** Gateways reject it.
- **A token may come from a command instead.** Set exactly one of the token
  and the token command; both or neither fails. The command prints the current
  token and must be safe to run repeatedly and concurrently. Its stderr is
  shown on failure, its stdout never. Whoever can run the command in the Sandbox can mint a token.
- **Specialists: the spawning Booley process runs the command.** It caches the
  result in memory for a few minutes and hands it to the Codex child as the
  static token, removing the command variable. A helper process could neither
  cache nor retry, and Codex runs its auth command several times per launch,
  multiplying runs of a command that may claim a token. A gateway rejection
  (HTTP 401) drops the cache, re-runs the command and retries once; a second
  rejection propagates. A token expiring mid-run is out of scope.
- **Interactive Codex: the helper runs the command, uncached.** No Booley
  process spawns that Codex. The command variable therefore stays visible to
  Codex shell tools so `booley` commands there still reach the gateway, while
  a static token is hidden from them.
- **Rejected: the MCP HTTP server as a token cache.** It would serve bearer
  tokens on loopback to any process in the Sandbox without running the command.

### What the registrar writes

For interactive Codex, the in-Sandbox registrar adds the gateway provider to
the user's Codex config and selects it. The provider table is Booley's;
the provider selection, model pin and `fast_mode = false` are written only
when absent, so the user's own keys win, with a warning when they defeat the
gateway. The token exclusion is added to any existing list.

### Text-only Specialists

Text-only Specialists such as the coverage analyst use the same provider and
token handling.

- **Booley generates the model-catalog entry** rather than reading Codex's
  models cache, which the native login the policy excludes fills. It does so
  outside the gateway too when the cache lacks the model. Tool fields are off
  and a short Booley instruction replaces Codex's coding-agent prompt. An
  unreadable, malformed or ambiguous cache still fails loud: that is a fault,
  not a missing model.
- **A text-only run without a nested tool allowlist gets the provider and no
  Booley MCP server**, so a run with no tools (Doctor's developer probe) works.
- **Rejected: refusing text-only runs under the gateway.** It would leave a
  gateway Sandbox with no coverage analysis.

### Claude never sees the gateway

In an issued Sandbox with Codex on the gateway, Booley blanks the gateway
variables for every Claude process it starts or configures and sets a marker
so Doctor can tell "hidden" from "never configured". Hand-written Sandboxes
keep their environment.

This is not an isolation boundary: VS Code terminals, lifecycle hooks and the
HTTP MCP server carry the variables, a same-user process can read another's
environment, and a Claude panel opened before the first start hook runs with
the values once. A short-lived token from the token command limits the
exposure. A `booley` command in Claude's shell cannot reach the gateway, while
Codex Specialists run by the MCP server still do.

The Claude-specific `ANTHROPIC_AUTH_TOKEN` is scrub-only: policies that
disallow it remove it from agent environments, and Booley never forwards it
into a Sandbox spec.

## Consequences

- VS Code resolves `localEnv` from its own process, so it must start from a
  shell that exports the gateway variables; the spec's initialize step refuses
  to open the Sandbox without them.
- Operator setup lives in [GATEWAY.md](../internals/GATEWAY.md).
