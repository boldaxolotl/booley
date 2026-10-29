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
