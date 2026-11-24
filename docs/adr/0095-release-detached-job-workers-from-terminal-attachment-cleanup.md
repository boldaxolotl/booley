# Release detached Job workers from terminal attachment cleanup

Status: accepted (2026-11-11 triage of
[#3396](https://github.com/boldaxolotl/booley/issues/3396))

Amends the lifecycle decision of
[ADR 0086](0086-launch-flows-and-specialists-from-the-dashboard.md)
("Standalone Jobs live as long as the Sandbox") for terminal attachments. Its
other decisions stand.

## Context

A command run through a `booley session` terminal attachment runs under an
execution supervisor. The supervisor adopts every orphaned descendant and ends
all of them when the command ends, so no stray EDA process outlives the
command while its Job slot looks free.

ADR 0086 added detached Standalone Job workers, which must outlive the process
that starts them. Started inside a terminal attachment, such a worker ended
with the attachment's command. Started from a VS Code Dev Containers terminal,
which has no supervisor, it kept running.

Two facts shape the decision:

- A detached worker carries no execution identity. Its Job slot is owned by
  its own process and recovered by that process's liveness, exactly like a run
  typed in a VS Code terminal.
- Recovery after a lost supervisor finds processes by execution identity, so
  it already leaves detached workers alone. Only the live supervisor ended
  them.

## Decision

1. **Release by recorded token.** When the detached Job spawn helper runs
   inside a terminal attachment, it records a fresh release token with that
   attachment's execution before it starts the worker, and gives the worker
   the token in its environment. When the command ends, the supervisor leaves
   alone every descendant that carries a token recorded for its execution,
   ends everything else, and exits. Released processes continue under the
   container's init process.
2. **Every ending.** Release holds when the command exits, on cancellation,
   on forced cancellation, on a lost host side, and on lease recovery. A
   detached Job stops when it finishes, when its process is signalled
   directly, or when the Sandbox stops.
3. **One exception.** Only the spawn helper releases. A process that starts
   its own session, or sets the variable without a recorded token, is ended as
   before. There is no user-facing way to release a process.
4. **Record unchanged.** The execution record keeps its format.
   `tree_terminal` means every process the execution still owns is gone. The
   recorded tokens are the trail of what was released.

## Rejected alternatives

- **Spawn from the HTTP MCP server.** It runs outside every attachment's
  process tree, so the supervisor would not change. Rejected: it is not
  started when the agent app is `none`, nothing restarts it, and it would be a
  second spawn path beside the direct one that VS Code terminals use.
- **Release by registered process ID.** A process under the worker whose
  parent exits is adopted by the supervisor and no longer sits below the
  worker, so it would be ended. The process ID is also known only after the
  worker starts.
- **Release by a fixed environment variable.** Any process could set it, which
  is the general escape this decision declines.
- **Keep the limit and document it.** That contradicts ADR 0086's lifecycle on
  one of the two Sandbox Attachments.

## Consequences

- The supervisor's guarantee that nothing survives the command has one named
  exception.
- `Ctrl-C` in a terminal attachment no longer stops a detached Job. No Booley
  command cancels a Standalone Job; ADR 0086 left that out of scope.
- A released Job with no attachment or editor counts as Sandbox activity only
  when it is a simulation
  ([#3487](https://github.com/boldaxolotl/booley/issues/3487)).
  `session refresh` and `session up --rebuild` stop it without the warning
  that `session down` gives
  ([#3488](https://github.com/boldaxolotl/booley/issues/3488)).
- The Goal baseline worker uses the same spawn helper and gets the same
  behavior until Goal Mode is removed.
