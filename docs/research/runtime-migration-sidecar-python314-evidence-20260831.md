# Python 3.14 sidecar migration evidence — 2026-08-31

This report covers Phase 3 of the runtime migration plan in
[#199](https://github.com/boldaxolotl/booley/pull/199) for
[#156](https://github.com/boldaxolotl/booley/issues/156). The isolated branch
starts from `main` at `b5ec64bbd17bd9e97fd04525268cb667e7ae74f5` and changes
only the Python base of the egress proxy, FlexNet relay, and idle reaper.

## Decision

**Promote the Phase 3 sidecar pins.** The complete local matrix and the fresh
implementation-PR build pass. This decision applies only to the three sidecar
images; it does not change the Phase 2 Session-Python hold or close #156.

## Immutable inputs

The official multi-platform tags and their `linux/amd64` manifests were
resolved on 2026-08-31 with `docker buildx imagetools inspect`. Both required
same-OS variants exist in the
[Docker Official Images Python source](https://github.com/docker-library/python/tree/master/3.14).

| Surface | Control | Candidate index digest | Candidate `linux/amd64` manifest |
| --- | --- | --- | --- |
| Egress proxy | `python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca` | `python:3.14.7-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63` | `sha256:6e9a7d1f48cf0127a5be29b58dba0c7f1b59c118619f011b7a6fca28d00adfd4` |
| FlexNet relay and reaper | `python:3.13.15-alpine3.24@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d` | `python:3.14.7-alpine3.24@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc` | `sha256:0ad7f98a97b1b8fcc226f5cbe49f0b95cd6f624582cdc6fbf7e41312075cb401` |
| Reaper Docker CLI stage | `docker:29.7.2-cli@sha256:000bb62ff495f986c9f5578eb67cc2cb98b91138eda81d7762d5371eb8a497fe` | Unchanged | Unchanged |

No sidecar distribution, application source, dependency, entrypoint, user, or
Docker CLI input changed with the Python pin.

## Test-first and behavior evidence

`tests/docker/test_reaper_image_e2e.py` was added before changing the pin. It
launched the existing `booley-reaper:latest` image and failed at the intended
boundary: the packaged runtime reported `Python 3.13.15`, not `Python 3.14.7`.
After the pin change, the exact plan command built the candidate without cache
and all three image-owned cases passed.

The test does not import `booley.docker.reaper`. It starts the packaged
entrypoint with a mounted Docker socket, gives it a disposable labeled Session
and deterministic licensed topology, and observes deletion of the Session,
relay, and both networks. A second candidate container uses an unreachable
`DOCKER_HOST`; over 4.5 seconds with a two-second interval it remains running,
logs two through four failed passes, and is stopped in `finally`.

The complete local behavior matrix was:

| Proof | Result |
| --- | --- |
| Proxy and reaper unit suites | 63 passed |
| Containerized proxy CONNECT, bidirectional bytes, and SIGTERM JSON stats | Passed |
| Candidate reaper image contract, owned cleanup, and bounded daemon retry | 3 passed |
| FlexNet healthcheck, two-port forwarding, isolation, hardening, and cleanup | 3 passed |
| Dockerfile pin and FlexNet static contracts | 66 passed |

The FlexNet lifecycle inspection confirmed the numeric `65532:65532` user,
read-only root filesystem, `ALL` capability drop, `no-new-privileges`, no host
port bindings, no mounts, and an internal isolated private bridge. Its real
healthcheck and fixed-destination byte flows passed on both configured ports.

## No-cache build and size evidence

All six images were built locally with `--pull --no-cache`. Every container
reported its expected exact Python patch. The final images contain one
`/usr/local` Python runtime, no `/usr/bin/python3`, no `/root/.cache`, no APK
cache, and no populated APT list cache.

| Sidecar | Control bytes | Candidate bytes | Delta | Delta % |
| --- | ---: | ---: | ---: | ---: |
| Egress proxy | 44,373,437 | 44,818,672 | +445,235 | +1.0034% |
| FlexNet relay | 16,906,031 | 17,773,901 | +867,870 | +5.1335% |
| Reaper | 36,449,990 | 37,317,873 | +867,883 | +2.3810% |

Full `docker history --no-trunc` comparisons attribute the changed bytes to
the official Python runtime layer. The Bookworm runtime layer grew while its
base, runtime-package, application-copy, workdir, expose, and entrypoint layers
retained the same roles and no new final layer appeared. The Alpine candidates
add `zstd-dev` only inside the upstream temporary build-dependency set; that
set is removed by the same layer before export. The Alpine root, CA/tzdata,
application-copy, workdir, healthcheck, user, and entrypoint structure is
unchanged. The reaper's copied Docker CLI layer remains 42.7 MB and uses the
same digest-pinned stage. There is no duplicate interpreter, retained package
cache, or build-only application layer.

The hosted workflow builds all three candidates with `--pull --no-cache`,
asserts `Python 3.14.7`, captures complete image inspect and history artifacts,
and runs the proxy, reaper, and FlexNet suites as a required `bwave-smoke`
step. [CI run 33398776175](https://github.com/boldaxolotl/booley/actions/runs/33398776175)
tested implementation head `f3849cfd438899accaa24550197871d6a3f34ab1`;
`bwave-smoke` passed in 5m07s, along
with required lint, docs, Rust, B-Wave integration, packaging, and Ubuntu and
Windows Python 3.11/3.13/3.14 jobs. The
[Docker evidence artifact](https://github.com/boldaxolotl/booley/actions/runs/33398776175/artifacts/9760500403)
is `docker-build-evidence` (ID `9760500403`) with archive digest
`sha256:c0a32ae9ea8627c9ecac0c9abf4a554e9ddd75ddc89854da4bee7d9c047b62c3`.
