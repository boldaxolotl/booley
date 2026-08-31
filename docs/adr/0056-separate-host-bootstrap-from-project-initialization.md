---
status: accepted
---

# Separate Host Bootstrap from Project Initialization

`booley init` historically interleaved Project-independent host preparation with
Project mutation and Session Runtime issuance. Booley exposes an idempotent
`booley bootstrap` command for Project-independent preparation while preserving
`booley init` as the one-command path: ordinary init reconciles Host Bootstrap
before any Project mutation, and aborts without creating Project state when
bootstrap fails.

Host Bootstrap verifies Git, Docker, and VS Code as required external
prerequisites, deploys Booley skills, prepares the base Session Image and shared
Nangate45 cache, and owns one host-wide egress network, proxy, and reaper. Their
policy lives in optional XDG-aware `~/.config/booley/config.toml`; absent
configuration selects built-in defaults, and Project `[interactive]` fields
fail with migration instructions rather than silently selecting global policy.
Host policy is strictly validated before any mutation. Policy or image drift
recreates sidecars only when no Session Runtime is active; otherwise
reconciliation fails with the active sessions and repair instructions.

Project Initialization retains agent provider and authentication policy,
Project state, Ticket Board, Git integration, selected or derived images, and
Session Runtime issuance. `init --seed` performs only a cheap
bootstrap-readiness check and directs an unready host to `booley bootstrap`.
`--force` reconciles managed resources despite freshness while preserving
caches and user-owned files. For both commands, `--check-only` returns 0 when
current, 1 when work is pending, and 2 when inspection cannot complete or the
environment is invalid. Host Bootstrap and Project Initialization reconcile
images through the same authoritative lifecycle module using explicit host and
Project scopes, and host-side Doctor consumes the same structured readiness.

## Considered options

- Requiring users to run bootstrap before every init was rejected because it
  weakens the existing one-command setup path and still cannot eliminate drift
  checks.
- Per-Project proxies and reapers were rejected as unnecessary duplication;
  Interactive Mode infrastructure and policy are host-wide.
- Automatically adopting a Project's old `[interactive]` values was rejected
  because Project order must not determine host security or capacity policy.

## Consequences

Host policy applies uniformly across Projects: the idle timeout and session cap
cover every Interactive Mode Session Runtime on the Docker daemon, and added
egress domains are reachable from every Project. Bootstrap validates but does
not install external applications, select an agent provider, edit host policy,
or read or write Project state.
