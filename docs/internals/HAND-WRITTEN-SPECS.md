# Hand-written devcontainer specs

Booley normally writes a Project's devcontainer spec with `booley init`. This
doc is for maintainers who write one by hand instead, such as the GitHub
Codespace of the [picorv32 demo](https://github.com/boldaxolotl/booley-prj-picorv32).
User docs cover only Booley-issued Sandboxes. Put the variables below in the
spec's `containerEnv`, so every process in the container sees them, including
a plain `docker exec`.

## Sandbox egress mode (`BOOLEY_EGRESS`)

Inside a Sandbox, Booley routes agents and tools through its egress proxy. A
container that can't reach that proxy, such as a Codespace, must declare
itself an **Unproxied Sandbox**:

| `BOOLEY_EGRESS` | Effect |
|---|---|
| unset or `proxy` | Default. Booley sets the `HTTP_PROXY` variables unless the environment already has them. |
| `unproxied` | Booley sets no proxy variables; agents and tools use the network the platform gives them. |
| anything else | `booley` and `booley-mcp` refuse to start (exit 2). `booley doctor` still runs and FAILs. |

Booley never guesses this from the platform (for example from `CODESPACES`).
The host `[sandbox] egress` setting decides the network of a Sandbox Booley
creates. `BOOLEY_EGRESS` must match it: Booley refuses to create a Sandbox
whose declaration differs, and
[Doctor reports a running one](../user/TROUBLESHOOTING.md#doctor-reports-a-sandbox-whose-egress-mode-differs-from-the-host).
In an Unproxied Sandbox Doctor WARNs `sandbox.unproxied`; to accept that,
[waive the check](../user/DOCTOR.md#doctor-waivers-doctor-waiverstoml).

## Agent app in a hand-written spec (`BOOLEY_AGENT_APP`)

`BOOLEY_AGENT_APP` names the agent app Booley connects to: `claude`, `codex`,
`none`, or `auto`. With `auto`, Booley decides at every Sandbox start from the
credentials present, and allows every CLI that has one:

1. Claude, if there is a Claude credential (`ANTHROPIC_API_KEY`,
   `CLAUDE_CODE_OAUTH_TOKEN`, a `booley auth` token, or a Claude login).
2. Codex, if there is a Codex credential (`OPENAI_API_KEY`, a
   `booley auth` token, or a Codex login).

With both credentials, both CLIs are registered and Claude is the default; with
one, only that CLI is. With neither, Claude starts and asks you to log in.

The other allowed CLI works as a plain `claude` or `codex` session in the Sandbox
terminal. `booley chat --cli` and `[models]` pins still follow
[`[agent] clis`](../user/CONFIG.md#agent-clis-agent) in `booley.toml`.

Booley makes the choice in its start-up step, so the spec's
`postStartCommand` (and `postCreateCommand`, if it has one) must run the
registrar module `booley.harness.incontainer_register` with `python -m`.

`booley doctor` in the Sandbox shows which CLIs `auto` allowed, which is the default, and why. If
[`[agent] default`](../user/CONFIG.md#agent-clis-agent) (or the older `provider`) pins an app, set
`BOOLEY_AGENT_APP` to that app or remove the pin. `auto` works only inside a
container.

## Project setup in a hand-written spec (`booley init`)

Run `booley init` in the spec's `onCreateCommand` to set up the Project
(guidance links, Dashboard task, Git settings, line endings). Project setup
needs no Booley module import; the registrar above is a separate start step.
A container counts as a Sandbox when `/.dockerenv` or `/run/.containerenv`
exists or `BOOLEY_CONTAINER=1` is set; set it in `containerEnv` for an image
that has neither file.

- In a hand-written Sandbox it does Project Initialization only: no Docker,
  image, Sandbox Issuance, pin, or inventory work.
- It is idempotent and exits 0 when nothing changes, so it is safe on every
  create.
- It never overwrites a default Agent CLI that `booley.toml` sets. Otherwise
  it fills one from `BOOLEY_AGENT_APP` (`claude` or `codex`); `auto`, `none`,
  or unset leave it unset, with a note.
- Once a default Agent CLI is set, it records `[agent] auth` from the
  credentials the Sandbox holds when that value is missing or `auto`
  (`subscription` when a CLI has both), and keeps an explicit one.
- `worktree.useRelativePaths` is `true` only when the container's Git is 2.48
  or newer, else `false`. A host Git sharing the bind-mounted checkout must
  then be 2.48+ too.
- `--seed`, `--force`, `--repin-booley-dir`, `--scaffold`, `--provider`, and
  `--auth` are host-only and refused here, as is a pending Booley Directory
  migration (run `booley init` on the host).
- A Booley-issued Sandbox refuses `booley init`; run it on the host.
