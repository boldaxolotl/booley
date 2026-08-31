# Spike release-channel research for issue #157

Date accessed: **2026-08-31**

## Recommendation

Use a **tested snapshot of the official
`riscv-software-src/riscv-isa-sim` `master` branch**, pinned by full 40-character
commit SHA. The dated candidate for issue #157 is
[`c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb`](https://github.com/riscv-software-src/riscv-isa-sim/commit/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb),
the upstream `master` tip observed on 2026-08-31. It passed the Booley
acceptance gates recorded below and is the adopted pin.

This is a recommendation/inference, not an upstream promise of stability.
Upstream has an active `master` and Ubuntu 24.04 CI, but it does not publish a
maintained stable release series. A fork would add another trust and update
boundary without solving that problem.

## Verified facts

### Booley's present state and requirement

- [Issue #157](https://github.com/boldaxolotl/booley/issues/157) says Booley
  currently uses a tested master snapshot because the formal release does not
  build on Ubuntu 24.04, and requires an exact pin, a successful RISC-V image
  build, the PicoRV32 demo, and Spike differential flows before moving it.
- The recipe currently pins
  [`55b4658dbf574ba0b714083ec436ce2cb5be1998`](https://github.com/riscv-software-src/riscv-isa-sim/commit/55b4658dbf574ba0b714083ec436ce2cb5be1998),
  verifies the fetched `HEAD`, builds from source, installs to `/opt/riscv`, and
  requires an executable `spike` before completing the layer
  ([Booley Dockerfile at the researched revision](https://github.com/boldaxolotl/booley/blob/a1c11fbf5a93cec758c779be9629cfb23fa3df48/src/booley/data/docker/Dockerfile.riscv#L93-L117)).
  The pinned upstream commit is dated 2026-06-26.

### Official releases are stale, not a maintained channel

- Upstream's [release list](https://github.com/riscv-software-src/riscv-isa-sim/releases)
  contains only `v1.0.0` (2019-04-01) and `v1.1.0` (2021-12-17). The latest
  formal release is
  [`v1.1.0` at `530af85d83781a3dae31a4ace84a573ec255fefa`](https://github.com/riscv-software-src/riscv-isa-sim/releases/tag/v1.1.0),
  with no release assets or release notes.
- Upstream documents SemVer intent for the ISA-facing public API, while
  explicitly excluding the C++ internal interface from its public API
  ([versioning statement](https://github.com/riscv-software-src/riscv-isa-sim/blob/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb/README.md#versioning-and-apis)).
  Nevertheless, both the current Booley snapshot and the candidate still
  identify themselves as
  [`1.1.1-dev`](https://github.com/riscv-software-src/riscv-isa-sim/blob/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb/VERSION).

### GCC 13 and Ubuntu 24.04

- The `v1.1.0` source predates upstream
  [PR #1284](https://github.com/riscv-software-src/riscv-isa-sim/pull/1284).
  Its exact fix commit,
  [`0a7bb5403d0290cea8b2356179d92e4c61ffd51d`](https://github.com/riscv-software-src/riscv-isa-sim/commit/0a7bb5403d0290cea8b2356179d92e4c61ffd51d),
  records the GCC 13 `uint64_t` failure and adds the missing `<cstdint>` include;
  it was merged to upstream `master` as
  [`f29dcd0d34bfc8c7d8982c9d03dc2e40bbc2f212`](https://github.com/riscv-software-src/riscv-isa-sim/commit/f29dcd0d34bfc8c7d8982c9d03dc2e40bbc2f212)
  on 2023-03-16.
- Ubuntu's official Noble package record identifies Noble as 24.04 LTS and
  supplies `g++-13` (13.2.0 in the release pocket, with 13.3.0 updates on
  amd64/i386)
  ([Ubuntu package record](https://packages.ubuntu.com/noble/g%2B%2B-13)).
- At both Booley's current pin and the proposed candidate, upstream's
  [continuous-integration workflow](https://github.com/riscv-software-src/riscv-isa-sim/blob/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb/.github/workflows/continuous-integration.yml)
  targets `ubuntu-24.04`. Its dependency list is `build-essential`,
  `device-tree-compiler`, `g++-riscv64-linux-gnu`, and
  `libc6-dev-riscv64-cross`
  ([apt package list](https://github.com/riscv-software-src/riscv-isa-sim/blob/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb/.github/workflows/apt-packages.txt)).
  Its build script configures Spike, builds with warnings enabled, runs
  `make check`, installs it, and executes `spike -h`
  ([`ci-tests/build-spike`](https://github.com/riscv-software-src/riscv-isa-sim/blob/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb/ci-tests/build-spike)).
  The following test script builds programs and `riscv-pk`, then exercises
  scalar, vector, atomic, library, custom-extension, custom-CSR, and DTB paths
  ([`ci-tests/test-spike`](https://github.com/riscv-software-src/riscv-isa-sim/blob/c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb/ci-tests/test-spike)).

### Maintenance and exact snapshot candidates

| Candidate | Exact revision | Verified evidence | Disposition |
|---|---|---|---|
| Latest formal release | `v1.1.0` / `530af85d83781a3dae31a4ace84a573ec255fefa` | Released 2021-12-17; predates the merged GCC 13 fix | Reject for Ubuntu 24.04 |
| Current Booley snapshot | `55b4658dbf574ba0b714083ec436ce2cb5be1998` | Upstream merge commit dated 2026-06-26; already pinned in Booley | Safe rollback/base candidate |
| Dated official-master candidate | `c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb` | Upstream merge commit dated 2026-08-28; 111 commits ahead of the Booley pin ([comparison](https://github.com/riscv-software-src/riscv-isa-sim/compare/55b4658dbf574ba0b714083ec436ce2cb5be1998...c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb)) | Adopted after all Booley gates passed |
| Candidate's tested PR head | `4d513dcf5cfd21c456bf3804e723cca318229d36` | PR #2396 head; its source tree (`40fbb5a0b4c46ef39652ae27ab185668aadff3e6`) equals the merge commit's source tree and passed the long-running Ubuntu build/test job ([PR](https://github.com/riscv-software-src/riscv-isa-sim/pull/2396), [Ubuntu CI run](https://github.com/riscv-software-src/riscv-isa-sim/actions/runs/33125441244/job/98702357942)) | Supporting upstream evidence, not the pin |

The candidate merge commit changes load/store exception behavior via
[PR #2396](https://github.com/riscv-software-src/riscv-isa-sim/pull/2396).
That makes Booley's differential-flow gate substantive: this is not merely a
packaging refresh.

### Fork screen

- The official RISC-V Software organization exposes the active
  [`riscv-isa-sim`](https://github.com/riscv-software-src/riscv-isa-sim)
  repository; no alternate official Spike release repository was found in that
  organization's repository inventory as of the access date
  ([organization inventory](https://github.com/orgs/riscv-software-src/repositories)).
- A plausible public fork,
  [`plctlab/plct-spike`](https://github.com/plctlab/plct-spike), identifies
  itself as a fork of official Spike, has no releases
  ([release list](https://github.com/plctlab/plct-spike/releases)), and its
  default `plct-master` tip is the 2020 commit
  [`958dcdc6fe6ed648444b622bbe667d6d477549ec`](https://github.com/plctlab/plct-spike/commit/958dcdc6fe6ed648444b622bbe667d6d477549ec).

## Inferences and policy

### Why a tested upstream-master snapshot is the least-bad channel

1. **Release channel:** unsuitable. `v1.1.0` is reproducible, but it is almost
   five years old at the access date and lacks the upstream GCC 13 fix.
   Ubuntu Noble's default GCC 13 makes that omission directly relevant.
   Carrying a local patch would create a Booley-maintained pseudo-release.
2. **Maintained fork:** no suitable fork was identified. The evaluated fork is
   materially older than upstream and offers neither releases nor an Ubuntu
   24.04 support contract.
3. **Official `master`:** upstream maintenance, the GCC fix, current ISA work,
   and Ubuntu 24.04 CI all converge here. A full SHA makes a development
   snapshot reproducible even though the branch itself moves.

### Required snapshot-update policy

1. Resolve `riscv-software-src/riscv-isa-sim/master` once at the start of an
   update and record the full commit SHA, commit date, and comparison from the
   previous pin. Never put `master`, a short SHA, or a moving ref in the
   Dockerfile.
2. Require upstream Ubuntu 24.04 evidence for the candidate's exact source
   tree. Prefer the successful pull-request build/test job that produced the
   merged tree; do not treat a green merge-commit badge alone as sufficient.
   The workflow iterates `git rev-list origin/master..HEAD`, so it is possible
   for a push build at the already-updated `origin/master` to have an empty
   iteration. This last sentence is an inference from the linked workflow.
3. Build `booley-sandbox-riscv` from a clean Ubuntu 24.04-compatible base. The
   Spike layer should run upstream `make check` in addition to requiring the
   installed executable. Record the compiler version, image digest, and
   `spike --version`/revision evidence.
4. Run and record all project acceptance gates named by issue #157: the
   PicoRV32 demo and every Spike differential flow, including expected output
   comparisons and exit status. A successful compile alone is insufficient.
5. Move `SPIKE_REF` only when every gate passes in the same candidate/image
   evaluation. Keep the previous SHA as the immediate rollback. If a gate
   fails, do not search forward commit-by-commit silently; open or link a
   compatibility finding and either fix it explicitly or retain the old pin.
6. Re-evaluate on a scheduled cadence (quarterly is a reasonable default) and
   when Booley needs a specific upstream ISA or correctness fix. A snapshot is
   not considered stale merely because `master` advanced; it becomes an update
   candidate only through the same gate.

Items 3-6 are Booley policy, not facts asserted by upstream.

## Validation record

The candidate was validated on 2026-08-31 from the exact revisions below:

- Booley base revision: `a1c11fbf5a93cec758c779be9629cfb23fa3df48`, with the issue
  changes to `SPIKE_REF` and the in-layer `make check` gate applied while
  building `Dockerfile.riscv`.
- Spike: `c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb`.
- PicoRV32: `a473fc8fca393771d83b0ffcf0b14db3393339d8`, including project-data
  revision `9a8adfadd572b4869acc46a3016036d6edf9d709`.

All required gates passed:

1. The complete `booley-sandbox-riscv` image built on Booley's pinned Ubuntu
   24.04 base with GCC `13.3.0`. The exported local image ID was
   `sha256:a3a80f4133ec4009ea0aab21adaa5ed3672e16f4f41c4a8f6800bab98790e4fe`,
   and the installed simulator identified itself as `1.1.1-dev`.
2. Spike built from the exact detached SHA and its upstream `make check` suite
   completed successfully inside that image.
3. The pinned PicoRV32 demo contract completed with `PicoRV32 demo contract
   passed` while the container had no network access.
4. A deterministic RV32IMC differential workload ran 4,096 arithmetic,
   multiply/divide/remainder, branch, store, and load iterations. Spike and
   PicoRV32 produced identical 16 KiB memory signatures; the comparison
   excluded only the HTIF `tohost` word that Spike clears after observing it.

The previous pin, `55b4658dbf574ba0b714083ec436ce2cb5be1998`, remains the immediate
rollback revision.
