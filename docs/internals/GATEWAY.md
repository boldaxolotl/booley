# Model gateway

The model gateway is an operator-run service that bills Codex model traffic
instead of an OpenAI login or API key; the Codespaces demo uses it. This
guide is for the operator who configures a gateway-backed Sandbox and for
contributors changing gateway code: the variables, the token contract, and
what Booley writes into Codex's config. The user-facing summary is
[Codex through a model gateway](../user/SANDBOX.md#codex-through-a-model-gateway),
and the decision and its security invariants are in
[ADR 0080](../adr/0080-route-codex-specialists-through-an-env-selected-gateway.md).

The gateway routes Codex only, never Claude Code. It covers Codex
[Specialists](../GLOSSARY.md) (including text-only ones such as
`coverage_analyst`), Booley's own calls when Codex is the default
[Agent CLI](../GLOSSARY.md), and interactive Codex in the Sandbox (`codex`,
`booley chat --cli codex`). The token is the only secret, and Booley never
writes it to a file or a log.

## Selecting the gateway

The gateway is honored only inside a Sandbox, so a host export cannot change
what a host-side run bills (`booley doctor` on the host warns about one).
There are two ways to select it:

- **Booley-issued Sandbox** (from `booley init`): declare
  `[agent.auth] codex = "gateway"` in `booley.toml`. A string
  `[agent] auth = "gateway"` is rejected while `claude` is in `[agent] clis`,
  and `claude = "gateway"` is always rejected. Sandbox Issuance records the
  per-CLI policy in `BOOLEY_AGENT_CLIS` and forwards `BOOLEY_GATEWAY_URL` plus
  the one token source the issuing shell exports, as `${localEnv:KEY}`
  references; it forwards no model or effort pin, Codex login, or API key.
  VS Code resolves `localEnv` from its own process environment, so start it
  from a shell that exports the variables: its `initializeCommand` refuses to
  open the Sandbox otherwise, and `booley session enter` or `run` names the
  missing variable. Host `booley doctor` from a shell without the exports
  warns `interactive.devcontainer-unverified`.
- **Hand-written Sandbox** (a Codespace or your own devcontainer): set
  `BOOLEY_PRIMARY_AUTH=gateway` and the variables below in its environment.
  With `BOOLEY_AGENT_APP=auto`, leave `BOOLEY_PRIMARY_AUTH` unset or `auto`
  instead: the gateway is then used only when the Sandbox has no Claude or
  Codex login (see
  [the agent app](../user/SANDBOX.md#agent-app-in-a-hand-written-spec-booley_agent_app)).

| Variable | Meaning |
| --- | --- |
| `BOOLEY_PRIMARY_AUTH=gateway` | Select the gateway in a hand-written Sandbox. |
| `BOOLEY_GATEWAY_URL` | The gateway's base URL, passed to Codex as is (for example `https://gw.example/v1`): `https://`, with no credentials, query, or fragment. |
| `BOOLEY_GATEWAY_TOKEN` | A static bearer token. Codex shell tools do not receive it. |
| `BOOLEY_GATEWAY_TOKEN_COMMAND` | A command that prints the current token, for short-lived tokens. Set exactly one of the two token sources. |

Codex must be in `[agent] clis`. In a hand-written Sandbox,
`BOOLEY_PRIMARY_AUTH=gateway` applies Sandbox-wide, so make Codex the
`[agent] default`. In a Booley-issued Sandbox the policy is per CLI, so the
default may be Claude, which keeps its own login.

In a Booley-issued Sandbox with `codex = "gateway"`, Booley hides the gateway
from Claude: it blanks every `BOOLEY_GATEWAY_*` variable in Claude's
`settings.json`, in Claude SDK runs, and in `booley chat --cli claude`, and
sets `BOOLEY_HIDDEN_GATEWAY=1` so Doctor and `booley auth --status` print a
NOTE instead of failing for a gateway they cannot see. This keeps the token
out of Claude's environment; it is not isolation, because anything in the
Sandbox that can run the token command can get a token (ADR 0080). A
hand-written Sandbox keeps its environment as set.

## Validation

`booley doctor` and `booley auth --status` fail and name the problem for a
missing URL, both or neither token source, a URL that is not plain `https`,
or a Project whose only Agent CLI is Claude ("routes Codex only"). In a
hand-written Sandbox, `booley auth --status` for Claude refuses the same way,
because the hand-off covers every CLI. Doctor also runs the token command and quotes its
stderr when it fails; that is a FAIL, or a WARN when `BOOLEY_AGENT_APP=auto`
chose the gateway or Codex is neither the default nor pinned for a Specialist
(the user may not have fetched a token yet). `booley auth --status` does not
run the command. A complete configuration passes Doctor.

## Token from a command

`BOOLEY_GATEWAY_TOKEN_COMMAND` is split into words like a shell would, but no
shell runs; wrap a pipeline in `sh -c '...'`. For a Codex Specialist, Booley
runs it, keeps the token in memory for 5 minutes, and passes it to the Codex
child. If the gateway rejects the token (HTTP 401), Booley drops the cached
token and retries the Specialist once with a fresh one. Interactive Codex
has no cache; see [Interactive Codex](#interactive-codex).

The command must:

- Print the current token on one line of printable ASCII.
- Be safe to run repeatedly and concurrently: the cache expiry, a 401
  refresh, every Doctor run, and every Codex auth call in an interactive
  session run it.
- Never print the token to stderr.

A non-zero exit, empty output, or no answer within 30 seconds fails, and
Booley shows the command's stderr as is, so use stderr for instructions such
as "run `gateway-login` first". Stdout is never shown.

## Interactive Codex

Booley does not start interactive Codex, so it configures Codex instead. On
every Sandbox start where Codex's policy is `gateway` (including a Sandbox
whose default is Claude), the in-Sandbox registrar edits the user's Codex
`config.toml` (the file that holds Booley's MCP entry) and keeps unrelated
keys:

- It adds `[model_providers.booley_gateway]` with the gateway URL and an
  `auth` command that runs `python -P -m booley.runtime.gateway_token`, which
  prints the token from Codex's environment, so no token, token command, or
  `auth.json` is written. Booley owns this table and replaces a differing
  one, so a changed URL is picked up.
- It sets `model_provider = "booley_gateway"`, `features.fast_mode = false`
  (the gateway rejects Codex's default priority tier), and `model` from
  `BOOLEY_GATEWAY_MODEL` when set, but only where the key is absent: the
  user's own keys win, and the registrar warns on stderr when
  `model_provider` selects another provider or `fast_mode` is `true`. To take
  a new model pin, delete `model`.
- It adds `BOOLEY_GATEWAY_TOKEN` to `shell_environment_policy.exclude` so
  Codex shell tools do not receive a static token.

An incomplete gateway environment prints a warning and skips the provider
(`gateway:invalid` in the registrar status); a layout the editor cannot
extend, such as an inline `model_providers = { ... }` table or a non-list
`exclude`, writes nothing (`gateway:skipped`). Other registration steps still
run.

When Codex's policy is not `gateway`, the registrar neither adds nor removes
the provider: after leaving the gateway, delete `model_provider`, `model`,
`features.fast_mode`, and `[model_providers.booley_gateway]` yourself, or
interactive Codex keeps routing to it. The exception is
`BOOLEY_AGENT_APP=auto`: a start that registers Codex on another policy
removes the `booley_gateway` table, plus `model_provider` and `model` while
they still hold the gateway's values, and keeps `features.fast_mode` and the
token exclusion, which route nothing. A start whose allowed Agent CLIs
exclude Codex leaves the Codex config alone.

The `auth` helper runs `BOOLEY_GATEWAY_TOKEN_COMMAND` itself on every call,
with no cache. Codex calls it about three times at startup and again after a
401 (measured in spike #2264), so a token obtained mid-session takes effect
at the next 401. A static `BOOLEY_GATEWAY_TOKEN` cannot change during a
session. The helper's timeout in `config.toml` is the command's 30 seconds
plus 15.

Codex shell tools still see `BOOLEY_GATEWAY_TOKEN_COMMAND`, as every Sandbox
shell does. So a `booley` command run from a Codex shell tool works with the
command, but with a static token it finds no token source and fails.

## Pinning one model and effort

A gateway often serves one fixed model, so two optional variables override
Booley's model choices. They apply only while Codex's policy is `gateway`
inside a Sandbox. `config.agent._gateway_pin` decides this, and Doctor's
`_check_gateway_model_pin` mirrors it; elsewhere both variables are ignored
and Doctor prints a NOTE.

| Variable | Effect |
| --- | --- |
| `BOOLEY_GATEWAY_MODEL` | One model ID (no whitespace or control characters), passed to Codex as is. It replaces the model of every Specialist that runs on Codex (a `gpt-*` `[models]` entry, or no entry while Codex is the default), and Booley's internal model when Codex is the default, including the model of Doctor's developer probe. A Specialist pinned to a `claude-*` model keeps it. |
| `BOOLEY_GATEWAY_EFFORT` | Reasoning effort for every Codex Specialist call and the developer probe: `none` (no reasoning, not "unset"), `minimal`, `low`, `medium`, `high`, `xhigh`, `max`, `ultra`, or `persistent`, case-insensitive. Unset, each call keeps its usual effort. Whether the gateway's model supports a level is the gateway's call. |

- The effort list is `CODEX_EFFORT_LEVELS` in `src/booley/config/gateway.py`,
  copied from Codex's `ReasoningEffort` enum
  (`codex-rs/protocol/src/openai_models.rs` at tag `rust-v0.160.0`, the
  release pinned in `src/booley/data/docker/agent-clis-package.json`). Codex
  also accepts free-form efforts; Booley does not, because the value goes
  into a `-c model_reasoning_effort="..."` override.
- An invalid value raises `GatewayConfigError` naming the variable, never its
  value. It fails `booley doctor` (`agent.credential-policy`),
  `booley chat`, and every Codex Specialist call before it starts.
- `booley chat` does not read either variable itself. The model pin reaches
  interactive Codex only through the Codex config (see
  [Interactive Codex](#interactive-codex)); the effort pin never does.
- Neither variable is secret.
