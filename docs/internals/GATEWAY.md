# Model gateway

The model gateway is an operator-run service that bills Codex model traffic
instead of an OpenAI login or API key; the Codespaces demo uses it. This is an
internal feature: user docs do not mention it. Read this when you run a
gateway-backed Sandbox or change gateway code. The variables and their
validation are in `src/booley/config/gateway.py`; the decision and its
security invariants are in
[ADR 0080](../adr/0080-route-codex-specialists-through-an-env-selected-gateway.md).

Scope:

- The gateway routes Codex only, never Claude Code. That covers Codex
  [Specialists](../GLOSSARY.md), Booley's own calls when Codex is the default
  [Agent CLI](../GLOSSARY.md), and interactive Codex in the Sandbox.
- It is honored only inside a Sandbox, so a host export cannot change what a
  host-side run bills.
- The token is the only secret, and Booley never writes it to a file or a log.

## Running a Booley-issued gateway Sandbox

Declare `[agent.auth] codex = "gateway"` in `booley.toml`.

- Add the gateway host to `[sandbox] egress_allowlist`, or use an Unproxied
  Sandbox.
- `BOOLEY_GATEWAY_TOKEN_COMMAND` runs inside the Sandbox, so it must name a
  command that exists there. Prefer it to a static token: a short-lived token
  limits what a Claude panel opened before the first start hook can see.
- The values come from the launching shell, like an API key. A headless
  `booley session` start refuses, naming the variable, when that shell lacks
  it. VS Code resolves `localEnv` from its own process, so start it from a
  shell that exports the variables. `booley session enter` into a VS Code-opened
  Sandbox does not carry them.
- A changed URL or static token behaves like a changed API key: Doctor fails
  with "active Sandbox differs from current host issuance" until the Sandbox is
  reissued.

## Leaving the gateway

When Codex's policy is not `gateway`, the registrar neither adds nor removes
the provider. After leaving the gateway, delete `model_provider`, `model`,
`features.fast_mode`, and `[model_providers.booley_gateway]` from Codex's
`config.toml` yourself, or interactive Codex keeps billing the gateway. The
exception is `BOOLEY_AGENT_APP=auto`, whose start removes the provider table
and the gateway-valued `model_provider` and `model`.

## Which processes see which variable

- Codex shell tools do not receive a static `BOOLEY_GATEWAY_TOKEN`: the
  registrar adds it to `shell_environment_policy.exclude`. They do see
  `BOOLEY_GATEWAY_TOKEN_COMMAND`, as every Sandbox shell does. So a `booley`
  command run from a Codex shell tool works with the command and fails with a
  static token.
- In a Booley-issued Sandbox, Claude's `settings.json` and Claude SDK runs get
  every `BOOLEY_GATEWAY_*` variable blanked. This keeps the token out of
  Claude's environment; it is not isolation, because anything in the Sandbox
  that can run the token command can get a token.

## Codex version

- The effort list is `CODEX_EFFORT_LEVELS` in `src/booley/config/gateway.py`,
  copied from Codex's `ReasoningEffort` enum
  (`codex-rs/protocol/src/openai_models.rs` at the release pinned in
  `src/booley/data/docker/agent-clis-package.json`; recheck it on a bump). Codex
  also accepts free-form efforts; Booley does not, because the value goes
  into a `-c model_reasoning_effort="..."` override.
