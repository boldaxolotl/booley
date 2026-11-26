# QA Host runbook

This runbook is for the Operator or the maintainer who needs to run a candidate
Booley build's host commands (`booley bootstrap`, `booley init`,
`booley session up`) without replacing the canonical host install. Use it for
a QA Run or GUI check of an unreleased build. Don't use it to test the
canonical install itself, or for a Codespace check
([codespaces/RUNBOOK.md](../codespaces/RUNBOOK.md)).

The QA Host is a privileged Docker-in-Docker container with its own Docker
daemon, its own `HOME` (`/home/qa`), and its own config home. Booley's base
Sandbox Image, `booley-proxy`, `booley-reaper`, and egress networks have fixed
names, but each name is local to one daemon. So the candidate gets its own
copies, and the canonical ones on the outer host keep their IDs
([ADR 0096](../../../docs/adr/0096-one-booley-install-per-docker-daemon.md)).
Two named volumes keep the inner daemon's images and the QA `HOME` between
runs.

| Name | Value |
|---|---|
| Container | `qa-host` |
| Image | `qa-host:<tag>`, built from this directory |
| Volumes | `qa-host-docker` (inner `/var/lib/docker`), `qa-host-home` (`/home/qa`) |
| Relay ports | inner `127.0.0.1:3780-3784`, outer `127.0.0.1:3890-3894` |
| Run as | `docker exec -u qa -e HOME=/home/qa -w <dir> qa-host bash -c 'export PATH=$HOME/.local/bin:$PATH; ...'`, written `qa$ ...` below |

## Rules

- **Get the maintainer's approval before step 1.** The QA Host is a
  privileged container, and a cold build uses about 20 GiB.
- **Treat the QA Host as root on the outer host.** `--privileged` gives it
  every capability and device. A Sandbox inside it keeps its own Sandbox
  Policy against the inner daemon, but an escape from that Sandbox lands in a
  privileged container, not in an unprivileged host user. Run only candidate
  builds of Booley's own repository there, and keep agent work in it to what
  the QA Run needs.
- **Never change Booley resources on the outer host.** Everything named
  `booley-*` there belongs to the canonical install or to other runs. Inside
  the QA Host, Booley's resources are the candidate's.
- **Run Booley as `qa` (uid 1000), never as root.** The Sandbox's user is uid
  1000, and Session Issuance refuses bind sources it doesn't own.
- **Keep the pairing link out of logs and findings.** It holds a token.

## 1. Stand up

1. Save the outer daemon's IDs: `qa/shared/qa-host/primary-ids.sh > evidence/qa-host/before.txt`.
2. Build the image and start the container:

   ```bash
   docker build -t qa-host:<tag> qa/shared/qa-host
   docker run -d --name qa-host --privileged --hostname qa-host \
     -v qa-host-docker:/var/lib/docker -v qa-host-home:/home/qa \
     -p 127.0.0.1:3890-3894:3780-3784 qa-host:<tag>
   ```

   Done when `docker exec qa-host docker info` succeeds. Outer ports 3890-3894
   are outside the relay range 3780-3879, so they never take a canonical
   relay's port.
3. Build a development wheel from the candidate commit (see
   [Building a development wheel][dev-wheel]), copy it in, and install it:

   ```bash
   docker cp <wheel> qa-host:/home/qa/ && docker exec qa-host chown qa:qa /home/qa/<wheel-file>
   qa$ uv tool install --force --reinstall /home/qa/<wheel-file>
   ```

   The install lands under `/home/qa/.local/share/uv/tools`, which passes the
   canonical-install check. Don't install under `/tmp` or a `qa-runs` path.
4. `qa$ booley bootstrap`. A fresh `qa-host-docker` volume builds the base
   Sandbox Image from scratch (see Measurements). Bootstrap warns that no
   `claude` or `codex` is on PATH; that only affects Project Setup on the QA
   host.
5. Create the Project and its Sandbox:

   ```bash
   qa$ mkdir -p /home/qa/<project> && cd /home/qa/<project> && git init -q && booley init
   qa$ booley session up
   ```

   For agent work in the Sandbox, first copy a credential into the QA config
   home, for example
   `docker cp ~/.config/booley/claude-oauth-token qa-host:/home/qa/.config/booley/`,
   then `chown qa:qa` and `chmod 600` it. `booley session up` prints
   `T3 Code relay: 127.0.0.1:<port>`.
6. Pair a browser on the outer host. Until `booley session up` prints a
   pairing link (#5438), ask T3 Code for one in the Sandbox, with the outer
   port as its base URL:

   ```bash
   qa$ booley session enter -- t3 auth pairing create \
     --base-dir /work/.booley/.runtime/t3 \
     --base-url http://127.0.0.1:<port + 110> --ttl 30m --json
   ```

   Open `pairUrl` on the outer host. For a link that Booley prints, change its
   port from `<port>` to `<port + 110>`. Done when T3 Code shows the Project.

## 2. Reuse

`docker rm -f qa-host` and the `docker run` above bring the QA Host back with
its images, install, and Projects. To test a new candidate, repeat step 1.3,
then run `qa$ booley bootstrap --update` (plain Bootstrap refuses a changed
install). It rebuilds only the layers that changed and replaces the inner
sidecars. A Sandbox stopped by the container's removal comes back with
`qa$ booley session up`.

## 3. Tear down

1. `qa$ booley session down` in each Project, then `docker rm -f qa-host`.
2. Save `primary-ids.sh` output to `evidence/qa-host/after.txt` and
   `diff` it with `before.txt`. Only `qa-host` rows may differ; report any
   other difference as a finding.
3. Keep the volumes for the next run. To remove everything, with the
   maintainer's approval:
   `docker volume rm qa-host-docker qa-host-home && docker image rm qa-host:<tag>`.

## Workarounds in the image

Each is a Booley behavior that the QA Host works around:

- **`/.dockerenv`:** Booley treats any process that sees it as inside a
  Sandbox and refuses host commands, so the entrypoint deletes it (#5584).
- **`python3-venv`:** a development wheel builds itself in a venv while
  building the Sandbox Image; without `ensurepip` Bootstrap fails only after
  building the runtime base (#5585).
- **Relay port:** the relay listens on the container's loopback. The
  entrypoint forwards ports 3780-3784 from the container's interface with
  `socat`.

`code-stub.sh` still stands in for a VS Code CLI that Host Bootstrap does not
need; #5634 removes it.

The reaper's fixed `/var/run/docker.sock` mount (#5565) is the inner daemon's
socket here, so it needs no workaround. It would on a rootless daemon.

## Measurements

From the spike on 2026-11-25 (24 CPUs, 32 GiB RAM, NVMe; candidate `5fbe80a78`
from `t3`):

| What | Value |
|---|---|
| QA Host image build | 35 s, 0.86 GB |
| Cold `booley bootstrap` | 19 min 28 s: 18 min 00 s for the runtime base and Nangate45, 1 min 28 s for the base Sandbox Image and sidecars |
| `booley init` + first `booley session up` | 14 s + 6 s |
| `qa-host-docker` volume after a cold build | 19.4 GB (6.8 GB images, 15.5 GB build cache, partly shared) |
| `qa-host-home` volume | 0.54 GB |
| Reuse: container recreated to Sandbox ready | 6 s |

[dev-wheel]: ../../../docs/internals/CONTRIBUTING.md#building-a-development-wheel
