# Model gateway

The model gateway is an operator-run service that bills Codex Specialist runs
instead of a native OpenAI credential. Today only Codex Specialists can use it,
and its current use is the Codespaces demo. The decision record is
[ADR 0080](../adr/0080-route-codex-specialists-through-an-env-selected-gateway.md).

## Setup

The policy is selected by the environment, not by `booley.toml`
(`[agent] auth = "gateway"` is rejected). Set these in the environment of a
Sandbox that does not come from `booley init`, such as a Codespace or a
hand-written devcontainer. Booley-issued Sandboxes take their policy from
`booley.toml`, and a host export is ignored (`booley doctor` warns about it).

| Variable | Meaning |
| --- | --- |
| `BOOLEY_PRIMARY_AUTH=gateway` | Select the gateway policy. |
| `BOOLEY_GATEWAY_URL` | The gateway's `https://` base URL, passed to Codex as is (for example `https://gw.example/v1`). No credentials in the URL. |
| `BOOLEY_GATEWAY_TOKEN` | The bearer token. It is never written to a config file, and Codex shell tools do not receive it. |
| `BOOLEY_GATEWAY_TOKEN_COMMAND` | A command that prints the current token, used instead of `BOOLEY_GATEWAY_TOKEN` when the token is short-lived. Set exactly one of the two. |

`[agent] provider` must be `codex`. Codex Specialists use the gateway; no
OpenAI or Anthropic credential and no Codex login reaches them. The gateway
host must be reachable from the Sandbox. Text-only Codex Specialists such as
`coverage_analyst` use the gateway too, on the pinned model. They need no Codex
models cache: Booley writes the model's catalog entry.

## Validation

A missing variable, both or neither token source, a non-`https` URL, or a
Claude provider fails `booley doctor` and `booley auth --status` naming the
problem. A failing token command fails `booley doctor`, which quotes the
command's stderr; `booley auth --status` does not run the command. A complete
configuration passes Doctor and exits 0.

## Token from a command

`BOOLEY_GATEWAY_TOKEN_COMMAND` is split into words like a shell would. No shell
runs; wrap a pipeline in `sh -c '...'`. Booley runs it when a Codex Specialist
needs a token and keeps the output in memory for about 5 minutes. If the
gateway rejects the token (HTTP 401), Booley runs the command once more and
retries the Specialist once.

The command contract:

- Print the current token on one line (ASCII, printable).
- Be safe to run repeatedly: the cache expiry, a 401 refresh, and every Doctor
  run each run it.
- Never print the token to stderr.
- A non-zero exit, no output, or no answer within 30 seconds fails with the
  command's stderr shown as is, so use stderr for instructions such as "run
  `gateway-login` first". Stdout is never shown.

The token is never logged or written to a file, but anything in the Sandbox that
can run the command can get a token.

## Pinning one model and effort

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
