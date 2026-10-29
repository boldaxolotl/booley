# Model gateway

The model gateway is an operator-run service that bills Codex Specialist runs
instead of a native OpenAI credential. Codex Specialists and the interactive
Codex in the Sandbox (including `booley chat`) can use it, and its current use
is the Codespaces demo. The decision record is
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
`coverage_analyst` do not support this policy yet.

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
retries the Specialist once. Interactive Codex has no such cache; see
[Interactive Codex](#interactive-codex).

The command contract:

- Print the current token on one line (ASCII, printable).
- Be safe to run repeatedly and concurrently: the cache expiry, a 401 refresh,
  every Doctor run, and every Codex auth call in an interactive session each
  run it.
- Never print the token to stderr.
- A non-zero exit, no output, or no answer within 30 seconds fails with the
  command's stderr shown as is, so use stderr for instructions such as "run
  `gateway-login` first". Stdout is never shown.

The token is never logged or written to a file, but anything in the Sandbox that
can run the command can get a token.

## Interactive Codex

On every Sandbox start under `BOOLEY_PRIMARY_AUTH=gateway`, the in-Sandbox
registrar edits the user's Codex `config.toml` (the same file as the MCP
entry), keeping unrelated keys:

- It adds `[model_providers.booley_gateway]` with the gateway URL and an
  `auth` command that runs `python -P -m booley.runtime.gateway_token`. Booley
  owns this table: a differing one is replaced, so a changed URL is picked up.
- It sets `model_provider = "booley_gateway"`, `features.fast_mode = false`
  (the gateway rejects the default priority tier) and, when
  `BOOLEY_GATEWAY_MODEL` is set, `model`. These are written only when absent;
  keys the user set win, and the registrar warns on stderr when
  `model_provider` selects another provider or `fast_mode` is `true`. The pin
  therefore applies only while `model` is unset: delete `model` to take a new
  pin. `BOOLEY_GATEWAY_EFFORT` is not written.
- It adds `BOOLEY_GATEWAY_TOKEN` to `shell_environment_policy.exclude` so
  Codex shell tools do not receive a static token.
- No token, token command or credential file is written. Under the gateway
  policy no `auth.json` is written either.
- A layout it cannot extend (such as a non-list `exclude`) is reported as
  `gateway:skipped` and nothing is written.
- Under any other policy the registrar neither adds nor removes providers.
  After leaving `gateway`, remove `model_provider`, `model`,
  `features.fast_mode` and `[model_providers.booley_gateway]` from the Codex
  config yourself, or interactive Codex keeps routing to the gateway.

Because Booley does not spawn this Codex, the in-memory cache and the 401 retry
of Specialist runs do not apply. The `auth` helper runs
`BOOLEY_GATEWAY_TOKEN_COMMAND` itself (Codex passes it the Sandbox
environment), once per helper run and with no cache: about three runs at TUI
start and another when Codex re-runs it after a 401 (spike #2264). A token
claimed mid-session is therefore used from the next 401 refresh. A static
`BOOLEY_GATEWAY_TOKEN` cannot change during a session. The helper's timeout in
`config.toml` is the command's 30 seconds plus a margin.

`BOOLEY_GATEWAY_TOKEN_COMMAND` stays visible to Codex shell tools, as it is to
every shell in the Sandbox. With a static `BOOLEY_GATEWAY_TOKEN`, a `booley`
command run from a Codex shell tool sees no token source and fails loud; with
the command it works.

An incomplete gateway environment prints a warning and skips the provider
(`gateway:invalid` in the registrar status), as does a TOML layout the editor
cannot extend, such as an inline `model_providers = { ... }` table
(`gateway:skipped`). Other registration steps still run, and `booley doctor`
reports the incomplete environment.

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
- The interactive `booley chat` session does not read either variable itself.
  The model pin reaches it only through the user's Codex config (see
  [Interactive Codex](#interactive-codex)); the effort is not written there.
- Under any other policy, outside a Sandbox, or with a non-Codex provider, both
  variables are ignored and Doctor prints a NOTE.
- The ADR 0080 invariants are unchanged: the pin adds no secret; only the gateway
  token is secret.
