# Declare Unproxied Sandboxes explicitly

Status: accepted

## Context

Booley forces an egress-proxy environment (`booley-proxy`) inside every
Sandbox. A Sandbox started by a platform that offers its own network, such as
a Codespace, has no such proxy, so every Booley command inherits dead proxy
variables and agent and model-service calls fail
([#2132](https://github.com/boldaxolotl/booley/issues/2132), part of
[#2118](https://github.com/boldaxolotl/booley/issues/2118)).

## Decision

A Sandbox declares `BOOLEY_EGRESS` in its spec's `containerEnv`. The accepted
values are exactly `proxy` and `unproxied`; unset means `proxy`. Under
`unproxied` Booley sets no proxy variable, so spawned agents and nested MCP
servers inherit none.

- The variable is read only inside a Sandbox (`BOOLEY_CONTAINER=1`).
- Opt-in is explicit. Booley never infers it from platform signals such as
  `CODESPACES`, and never writes the variable itself.
- An invalid value makes `booley` and `booley-mcp` refuse to start. `booley
  doctor` is exempt so it can report the invalid value as a FAIL.
- Doctor WARNs `sandbox.unproxied` under `unproxied`. The check ID is waivable,
  so an accepted Unproxied Sandbox stays visible without failing Doctor.
- Under `proxy`, Doctor FAILs when the effective proxy host does not resolve,
  and names the fix. It checks resolution only, not reachability.
- The variable is a declaration, not the security boundary. Any process in a
  Sandbox can set it for itself. In a Booley-issued Sandbox the internal egress
  network still denies direct egress, so flipping it only drops that process's
  proxy variables. The only effect on Doctor of a self-set value is turning the
  unresolved-proxy FAIL into the waivable WARN for that run.
- The release Simulation self-test runs the image under `--network none` and
  requires zero Doctor failures, so it declares `BOOLEY_EGRESS=unproxied`;
  that container has no egress at all.

A host-wide `[sandbox] egress` setting that issues such specs follows in
[#2133](https://github.com/boldaxolotl/booley/issues/2133).

## Rejected alternatives

**Auto-detect from platform signals.** `CODESPACES` and similar variables are
ambient and spoofable, and a wrong guess silently removes the proxy from a
Sandbox that should have one. Explicit declaration keeps the default-deny
posture the safe default.

**Silent fallback when the proxy is unreachable.** A Sandbox would lose
mediation without anyone choosing it. Doctor FAILs instead and names the
declaration that fixes it.
