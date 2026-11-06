# Run each Specialist on the Agent CLI its model implies

Status: accepted

## Context

A Project configured one provider, Claude or Codex, and every agent call ran on
it: the Developer Agent, each Specialist and Booley's own calls. `[models]`
chose models by tier (`heavy`, `standard`, `light`), `[models.roles]` pinned
one Specialist to a tier or a model, each Specialist declared a minimum tier
(`min_model`), and `booley specialist <name> --model` picked a tier per run.
In practice every Specialist resolved to the `standard` tier, so the tier layer
added indirection without choice. It also could not express the case users
asked for: a reviewer on Codex next to a mutation tester on Claude Code, each
billed its own way. A Codex model pinned on a Claude Project was passed to the
wrong CLI.

[#2753](https://github.com/boldaxolotl/booley/issues/2753) is the first slice
of the multi-CLI epic.

## Decision

- **Allowed set and default.** `[agent] clis` lists the Agent CLIs a Project
  may run (`claude`, `codex`); `[agent] default` picks the one the Developer
  Agent and Booley's own calls run on. `provider` stays as an alias of
  `default`; both set and different fails loading. Without `default`, the first
  entry of `clis` is the default; without `clis`, the set is the default alone.
  A default from `BOOLEY_PRIMARY_PROVIDER` or `BOOLEY_AGENT_APP` outside the
  set fails loading, naming its source. Every loader and Doctor resolve the
  default in one order: `BOOLEY_PRIMARY_PROVIDER`, the toml, `BOOLEY_AGENT_APP`.
- **Per-CLI auth.** `[agent] auth` is either one policy for every listed CLI
  or an `[agent.auth]` table keyed by CLI; an unlisted table key fails loading
  and an omitted CLI is `auto`. `gateway` is accepted for Codex only (a string
  `gateway` fails while `claude` is listed) and, as with the
  `BOOLEY_PRIMARY_AUTH=gateway` hand-off, is honored only inside a Sandbox.
- **The model implies the CLI.** `[models]` has one key per Specialist
  (`reviewer`, `mutation_tester`, `coverage_analyst`). `claude-*` models run on
  Claude Code and `gpt-*` models on Codex; that CLI must be in `clis`. An
  unknown key, a model with neither prefix, or a CLI outside the set fails
  loading, naming the key. A Specialist without an entry runs on the default
  CLI's built-in Specialist model (`claude-opus-5-5` / `gpt-6.1-sol`, today's
  `standard` tier); Booley's internal calls use `claude-sonnet-5-5` /
  `gpt-6-luna`.
- **Tiers retired with a migration error.** The tier keys, `[models.roles]`,
  `min_model` and the Specialist `--model` flag are removed. A config that
  still has a tier key or `[models.roles]` fails loading with the equivalent
  `[models]` lines (and the `[agent] clis` line when a pin needs the other
  CLI); `heavy` and `light` overrides have no equivalent and are dropped.
- **Strict selection.** A Specialist runs its one resolved model. A model the
  CLI refuses fails the call naming the `[models]` key; no other model is
  tried. Deep Doctor probes each resolved model through its own CLI. A
  list-of-models form with fallback is deferred to
  [#2836](https://github.com/boldaxolotl/booley/issues/2836).
- **One backend per CLI.** An agent call names its CLI; the runtime builds
  each CLI's backend once, with that CLI's auth policy.

## Order of delivery

A Booley-issued Sandbox seeding exactly the listed CLIs' credentials lands in
[#2755](https://github.com/boldaxolotl/booley/issues/2755), and per-CLI Doctor
credential checks in [#2754](https://github.com/boldaxolotl/booley/issues/2754).
Until #2755, host Sandbox Issuance (and every host reader of the Project's
credential policy) refuses a toml `gateway` policy, pointing to a hand-written
spec with `BOOLEY_PRIMARY_AUTH=gateway`, and a Booley-issued Sandbox carries
credentials for the default CLI only: a Specialist pinned to the other CLI
fails its first call with that CLI's auth error.

## Rejected alternatives

**Keep tiers and add a CLI per tier.** Every Specialist uses the same tier, so
a CLI per tier would still pin all Specialists together.
**A separate `cli` key per Specialist.** It can disagree with the model; the
model prefix already names the CLI.
**Fall back to another model on refusal.** It silently changes what a review
measured and what it bills; the user decides via the deferred list form.
**Keep `[models.roles]` as an alias.** Two spellings for one setting, one of
them accepting tier names that no longer exist.

## Consequences

- This supersedes ADR 0080's "`[agent] auth` keeps rejecting `gateway`"
  bullet: `[agent.auth] codex = "gateway"` is valid and honored inside a
  Sandbox. URL and token still come only from the environment.
- Projects with tier keys or `[models.roles]` must migrate before Booley loads
  their config; the error message gives the replacement.
- `booley init` still records `provider`; it keeps an `[agent.auth]` table it
  finds.
