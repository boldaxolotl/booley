---
status: accepted
---

# One canonical Booley install per Docker daemon

[ADR 0056](0056-separate-host-bootstrap-from-project-initialization.md) gives
Host Bootstrap one host-wide egress network, proxy, and reaper, and one base
Sandbox Image. Their names are fixed. A second Booley build that bootstraps on
the same Docker daemon rebuilds that base image and replaces those sidecars, so
it changes every Sandbox on the machine. QA and GUI checks of a candidate build
therefore had to swap the canonical install and wait for an idle host.

Booley keeps the fixed names and makes the Docker daemon the unit of
installation: each Docker daemon serves exactly one canonical Booley install,
recorded in that install's config home. A second build runs against its own
daemon, with its own `HOME` and config home, and owns that daemon's base image,
sidecars, networks, idle sweep, and session cap. For QA this is the QA Host, a
privileged Docker-in-Docker container with a persistent Docker data volume
(`qa/shared/qa-host/RUNBOOK.md`).

## Considered options

- Naming the base image, sidecars, and networks per build was rejected. It
  would amend ADR 0056 and touch about ten modules that hardcode the names,
  and two builds would still share one reaper sweep and session cap.
- Rootless Docker as the second daemon was kept only as a fallback. The reaper
  bind-mounts the fixed root socket path, so on a rootless daemon it would
  watch the wrong daemon until #5565 is fixed. Inside Docker-in-Docker that
  path is the inner daemon.
- Swapping the canonical install for each check, the previous practice, was
  rejected: it needs an idle host and replaces machine-global resources.

## Consequences

- Host-wide policy in ADR 0056 is per daemon: the idle timeout, session cap,
  and egress allowlist cover the Sandboxes of one daemon.
- A candidate build pays a cold base image build on its own daemon once. The
  QA Host's data volume keeps it for later runs of the same build.
- A host can itself be a container, so Booley must not treat every container
  as a Sandbox (#5584). The QA Host also supplies Host Bootstrap's external
  prerequisites; its runbook lists the workarounds.
- The outer host reaches the QA Host's T3 Code relay through a forwarded port,
  so a pairing link's port is rewritten.
