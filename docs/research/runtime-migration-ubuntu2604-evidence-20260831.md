# Ubuntu 26.04 migration evidence — 2026-08-31

This report records the Phase 4 precondition and OpenROAD probe results from
the runtime migration plan in
[#199](https://github.com/boldaxolotl/booley/pull/199) for
[#156](https://github.com/boldaxolotl/booley/issues/156). The original
evaluation used `main` at `34266dbb186d39eef40a2b64a2c36f00cb9b3c74`.
The follow-up review used current `main` at
`d5684d9a7c932cd62a18376a7b112f0d2ccb8a6e`, which includes merged
[#201](https://github.com/boldaxolotl/booley/pull/201) and the separate
[Phase 2 hold report](https://github.com/boldaxolotl/booley/blob/d5684d9a7c932cd62a18376a7b112f0d2ccb8a6e/docs/research/runtime-migration-python314-evidence-20260831.md)
from PR #207.

## Decision

**Hold. Keep Ubuntu 24.04 in production.**

One required precondition fails before the Phase 4 empty-cache EDA build is
permitted: the live Noble/deadsnakes channel cannot supply the Python 3.14.4
patch used by Ubuntu 26.04. It currently publishes `3.14.6-1+noble1` for
Noble/AMD64. Deadsnakes did publish `3.14.4-1+noble1`, but that package was
superseded and removed on 2026-05-15
([exact Launchpad publication record](https://api.launchpad.net/1.0/~deadsnakes/+archive/ubuntu/ppa/+binarypub/241600917)).
The required `U - S` exact-patch comparison therefore cannot be constructed
from the existing live channel.

The historical OpenROAD Debian package also fails its Ubuntu 26.04 native-APT
probe, as retained below, but that package is no longer the current channel.
[#154](https://github.com/boldaxolotl/booley/issues/154) closed on 2026-08-31
when PR #201 merged. Current `main` pins the official OpenROAD 26Q3-source OCI
artifact by immutable digest and records its licensing and Ubuntu 24.04 flow
validation in the
[channel decision](https://github.com/boldaxolotl/booley/blob/81cc8ca4db5bcc555f88dcf213c49be4980029b2/docs/research/openroad-26q3-channel-20260831.md).
The old `.deb` failure is consequently migration evidence, not an unresolved
issue-#154 blocker; the selected 26Q3 channel still needs Phase 4 validation in
the eventual `U` candidate.

No Ubuntu, Python, OpenROAD, compiler, path, or production-image pin changed
as part of this report.

## Noble/deadsnakes control precondition

The Noble control was probed from Booley's previously pinned Ubuntu 24.04
image, `ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517`.
The disposable probe ran from `2026-08-31T15:21:18Z` through
`2026-08-31T15:21:43Z` and used the same `add-apt-repository` invocation as
Booley's runtime base:

```shell
docker run --rm \
  ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 \
  bash -c 'set -e
    export DEBIAN_FRONTEND=noninteractive
    apt-get update
    apt-get install -y --no-install-recommends software-properties-common ca-certificates
    add-apt-repository -y ppa:deadsnakes/ppa
    apt-get update
    apt-cache policy python3.14 python3.14-venv python3.14-dev
    apt-cache madison python3.14 python3.14-venv python3.14-dev'
```

The exact relevant live-index output was:

```text
python3.14:
  Installed: (none)
  Candidate: 3.14.6-1+noble1
  Version table:
     3.14.6-1+noble1 500
        500 https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu noble/main amd64 Packages
python3.14-venv:
  Installed: (none)
  Candidate: 3.14.6-1+noble1
  Version table:
     3.14.6-1+noble1 500
        500 https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu noble/main amd64 Packages
python3.14-dev:
  Installed: (none)
  Candidate: 3.14.6-1+noble1
  Version table:
     3.14.6-1+noble1 500
        500 https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu noble/main amd64 Packages
```

Launchpad's first-party API independently reports the sole currently
published Noble source as `3.14.6-1+noble1`
([live Published-source query](https://api.launchpad.net/devel/~deadsnakes/+archive/ubuntu/ppa?ws.op=getPublishedSources&source_name=python3.14&distro_series=https%3A%2F%2Fapi.launchpad.net%2Fdevel%2Fubuntu%2Fnoble&status=Published)).
The historical `3.14.4-1+noble1` source was published on 2026-04-08
([Launchpad source record](https://api.launchpad.net/devel/~deadsnakes/+archive/ubuntu/ppa/+sourcepub/18297036)),
but the exact AMD64 binary record says it was superseded on 2026-05-14 and
removed on 2026-05-15. An APT consumer of the live PPA therefore cannot pin
that removed build.

## Immutable Ubuntu 26.04 input

The official tag was resolved with
`docker buildx imagetools inspect ubuntu:26.04`. The registry reported:

| Input | Value |
| --- | --- |
| Official index | `ubuntu:26.04@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b` |
| `linux/amd64` manifest | `sha256:889d056d5c6c0bfb55789ff3710681d68e50713cb562d2196dc07110599c7a6f` |
| AMD64 source | [`cloud-images/+oci/ubuntu-base`](https://git.launchpad.net/cloud-images/+oci/ubuntu-base) |
| AMD64 image revision | `8f6508c5aeaafe560ec725916fd2c86b4f6a5190` |
| AMD64 manifest creation annotation | `2026-08-11T00:00:00Z` |

The exact digest, rather than the mutable tag, was then pulled between
`2026-08-31T15:19:56Z` and `2026-08-31T15:19:58Z`:

```shell
docker pull \
  ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b
docker image inspect \
  ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b \
  --format 'RepoDigests={{json .RepoDigests}} Created={{.Created}} Architecture={{.Architecture}} OS={{.Os}}'
```

The retained pull and inspect output matches the requested digest:

```text
Digest: sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b
Status: Image is up to date for ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b
RepoDigests=["ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b"] Created=2026-08-17T09:00:47.315779976Z Architecture=amd64 OS=linux
```

The bounded probe ran by the same digest from `2026-08-31T15:20:27Z`
through `2026-08-31T15:20:46Z`. After `apt-get update`, its exact package
candidate command was:

```shell
docker run --rm \
  ubuntu@sha256:2260313b31c8c011cd2eebe728008efac1b3982be73eb71348ea2648d2c0e09b \
  bash -c 'set -u
    export DEBIAN_FRONTEND=noninteractive
    cat /etc/os-release
    apt-get update
    apt-cache policy python3 python3.14 python3.14-venv python3.14-dev gcc g++ binutils cmake libc6'
```

The relevant native candidates were:

| Package | Candidate |
| --- | --- |
| `python3` | `3.14.3-0ubuntu2` |
| `python3.14`, `python3.14-venv`, `python3.14-dev` | `3.14.4-1ubuntu0.1` |
| `gcc`, `g++` | `4:15.2.0-5ubuntu1` |
| `binutils` | `2.46-3ubuntu2` |
| `cmake` | `4.2.3-2ubuntu2` |
| installed and candidate `libc6` | `2.43-2ubuntu2.3` |

Canonical's
[USN-8509-1](https://ubuntu.com/security/notices/USN-8509-1) and
[Ubuntu package record](https://packages.ubuntu.com/resolute/python3.14)
independently identify the Resolute Python packages as
`3.14.4-1ubuntu0.1`. Launchpad retains the corresponding
[security publication](https://api.launchpad.net/devel/ubuntu/+archive/primary/+sourcepub/18600612)
and
[updates publication](https://api.launchpad.net/devel/ubuntu/+archive/primary/+sourcepub/18600799).

The plan requires `S` and `U` to report the same Python patch so that `U - S`
isolates Ubuntu and the compiler stack. Python 3.14.4 is the required `U`
patch, not a substitution. The blocker is that the existing live Noble PPA no
longer supplies that patch for `S`; directly comparing `U` with the Python
3.13 baseline, using the live Noble 3.14.6 build, or building a private 3.14.4
package would test a different matrix row.

## Historical OpenROAD Debian-package probe

This section records why the old production `.deb` cannot be carried into
Ubuntu 26.04. It does not supersede the 26Q3 OCI channel selected by PR #201.
The digest-based Ubuntu probe above downloaded
`openroad_2.0-17598-ga008522d8_amd64-ubuntu-22.04.deb` from the
[upstream release](https://github.com/Precision-Innovations/OpenROAD/releases/tag/2024-12-14),
verified SHA-256
`40ed178396b0276a5d5dfbbe695c9de9aac9088157a6655be02b39a0cef07207`,
and ran these exact relevant commands:

```shell
apt-get update
apt-get install -y --no-install-recommends ca-certificates curl
curl --proto '=https' --tlsv1.2 -fsSL \
  https://github.com/Precision-Innovations/OpenROAD/releases/download/2024-12-14/openroad_2.0-17598-ga008522d8_amd64-ubuntu-22.04.deb \
  -o /tmp/openroad.deb
sha256sum /tmp/openroad.deb
dpkg-deb --info /tmp/openroad.deb
dpkg-deb -f /tmp/openroad.deb Depends
set +e
apt-get install -y --no-install-recommends /tmp/openroad.deb
status=$?
set -e
echo "APT_INSTALL_EXIT=$status"
```

The exact checksum and `dpkg-deb --info` output were:

```text
40ed178396b0276a5d5dfbbe695c9de9aac9088157a6655be02b39a0cef07207  /tmp/openroad.deb
 new Debian package, version 2.0.
 size 56971996 bytes: control archive=83677 bytes.
     593 bytes,     7 lines      control
  295018 bytes,  3362 lines      md5sums
      83 bytes,     2 lines      shlibs
      72 bytes,     2 lines      triggers
 Package: openroad
 Version: 2.0-17598-ga008522d8
 Architecture: amd64
 Maintainer: Vitor Bandeira <vvbandeira@precisioninno.com>
 Installed-Size: 249310
 Depends: libc6 (>= 2.35), libgcc-s1 (>= 7), libgomp1 (>= 6), libpython3.10 (>= 3.10.0), libqt5charts5 (>= 5.7.1), libqt5core5a (>= 5.15.1), libqt5gui5 (>= 5.14.1) | libqt5gui5-gles (>= 5.14.1), libqt5widgets5 (>= 5.11.0~rc1), libstdc++6 (>= 12), libtcl8.6 (>= 8.6.0), tcl-tclreadline (>= 2.3.8), zlib1g (>= 1:1.1.4)
 Description: OpenROAD is an integrated chip physical design tool that takes a design from synthesized Verilog to routed layout.
```

The exact dependency-field command produced:

```text
libc6 (>= 2.35), libgcc-s1 (>= 7), libgomp1 (>= 6), libpython3.10 (>= 3.10.0), libqt5charts5 (>= 5.7.1), libqt5core5a (>= 5.15.1), libqt5gui5 (>= 5.14.1) | libqt5gui5-gles (>= 5.14.1), libqt5widgets5 (>= 5.11.0~rc1), libstdc++6 (>= 12), libtcl8.6 (>= 8.6.0), tcl-tclreadline (>= 2.3.8), zlib1g (>= 1:1.1.4)
```

Native APT exited 100 with this exact relevant error, including every named
Qt5 and Tcl dependency:

```text
The following packages have unmet dependencies:
 openroad : Depends: libgomp1 (>= 6) but it is not going to be installed
            Depends: libpython3.10 (>= 3.10.0) but it is not installable
            Depends: libqt5charts5 (>= 5.7.1) but it is not going to be installed
            Depends: libqt5core5a (>= 5.15.1)
            Depends: libqt5gui5 (>= 5.14.1) or
                     libqt5gui5-gles (>= 5.14.1) but it is not going to be installed
            Depends: libqt5widgets5 (>= 5.11.0~rc1)
            Depends: libtcl8.6 (>= 8.6.0) but it is not going to be installed
            Depends: tcl-tclreadline (>= 2.3.8) but it is not going to be installed
E: Unable to satisfy dependencies. Reached two conflicting assignments:
   1. openroad:amd64=2.0-17598-ga008522d8 is selected for install
   2. openroad:amd64 Depends libpython3.10 (>= 3.10.0)
      but none of the choices are installable:
      [no choices]
APT_INSTALL_EXIT=100
```

Because installation failed, `ldd`, `openroad -version`, and the `synth`
Booley Flow smoke test in physical mode could not run against this historical
package.

## Actions deliberately not taken

This evaluation did not:

- add a Jammy, Noble, deadsnakes, or other obsolete or mixed-distribution
  repository to Ubuntu 26.04;
- substitute compatibility packages or manually extract libraries;
- resurrect the removed Noble/deadsnakes Python 3.14.4 package or build a
  private replacement;
- mutate the production Dockerfile and continue through a knowingly invalid
  empty-cache EDA build; or
- compare image size, glibc symbols, or APT inventories against the wrong
  Python 3.13 or Python 3.14.6 control.

## Resume condition

Resume Phase 4 when Phase 2 can produce an Ubuntu 24.04 `S` image with the
exact Python patch supplied natively by Ubuntu 26.04 at the time of the rerun.
Issue #154 and the OpenROAD-channel selection are already resolved by PR #201;
do not wait for that closed issue or return to the historical `.deb`.

Then rebuild `S` and `U` from the same current `main`, integrate the selected
digest-pinned OpenROAD 26Q3 channel into `U`, record every APT package, perform
the empty-cache EDA and RISC-V builds, run the real `synth` Booley Flow in
physical mode, inspect all native glibc requirements, and compare the stable
base, Session Image, and RISC-V flavor. Until then, this Phase 4 hold is
intentional and does not close #156.
