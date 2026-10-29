# Model gateway

## Pinning one model and effort

Maintainer notes for the model and effort pin under the gateway policy.

The pin applies only when `BOOLEY_PRIMARY_AUTH=gateway` is honored (inside a
Sandbox) and `[agent] provider` is `codex`. `config.agent._gateway_pin` is the
single place that decides this; `booley doctor` mirrors the condition in
`_check_gateway_model_pin`. Two optional environment variables:

| Variable | Effect |
| --- | --- |
| `BOOLEY_GATEWAY_MODEL` | One model id (no whitespace or control characters), passed to Codex as is. Replaces every `[models]` tier, every `[models.roles]` pin and every Specialist minimum-tier floor, including the model Doctor's developer probe uses. |
| `BOOLEY_GATEWAY_EFFORT` | Reasoning effort for every Codex Specialist call and the mutation creator and developer probe calls: `none` (no reasoning, not "unset"), `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra` or `persistent` (case-insensitive). Unset, each call keeps its usual effort. Whether the gateway's model supports a level is the gateway's call. |

Notes:

- The effort list is `CODEX_EFFORT_LEVELS` in `src/booley/config/gateway.py`,
  taken from Codex's `ReasoningEffort` enum (`codex-rs/protocol/src/openai_models.rs`
  at tag `rust-v0.160.0`, the release pinned in
  `data/docker/agent-clis-package.json`). Codex also accepts free-form custom
  efforts; Booley does not, because the value reaches a `-c
  model_reasoning_effort="..."` override.
- An invalid value of either variable raises `GatewayConfigError` naming the
  variable, never its value. It fails `booley doctor`
  (`agent.credential-policy`), `booley chat` and every Codex Specialist call
  before it starts.
- The interactive `booley chat` session uses neither variable.
- Under any other policy, outside a Sandbox, or with a non-Codex provider, both
  variables are ignored and Doctor prints a NOTE.
- The ADR 0080 invariants are unchanged: the pin adds no secret; only the gateway
  token is secret.
