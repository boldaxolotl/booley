# Follow tested Spike master snapshots

The RISC-V image builds Spike from official upstream `riscv-isa-sim` master, pinned by full commit SHA.
Upstream's only releases are `v1.0.0` (2019) and `v1.1.0` (2021), and `v1.1.0` no longer compiles with
the image's GCC. Master is the maintained line: upstream's `Continuous Integration` workflow builds and
tests each pull-request commit before merge, and its `Debug Quick Test` workflow builds every master
commit and runs the debug tests against it.

A refresh selects the newest master commit whose `Debug Quick Test` push run succeeded, not whatever
happens to be HEAD. The `Continuous Integration` run on a master push is not evidence: it only tests
commits not yet on master, so it passes without building anything. The pin is refreshed monthly in
its own dependency-update PR, independent of Booley releases, and sooner when a Spike defect affects a
Booley user or a security fix requires it. A release never refreshes it: it ships the pin already on
`main`.

A candidate pin is accepted only after both Spike checks pass:

- the RISC-V substrate build on Booley's Ubuntu 26.04 base, including upstream `make check`; and
- the Spike probe in `.github/contracts/session-runtime.toml`, which runs an independently checked
  RV32 program and loads an extension library through `--extlib`.

Upstream CI runs on Ubuntu 24.04 and macOS only, so Booley's own image build is the Ubuntu 26.04
evidence. The PicoRV32 demo does not run Spike; it guards the rest of the rebuilt RISC-V image and
already runs against every release candidate, so it is not a separate refresh gate.

A newer commit that fails acceptance is held at the current pin, with a follow-up issue naming the
failure.

---
status: accepted
---

## Considered Options

- Pinning `v1.1.0` was rejected because it predates upstream's header fixes and does not build on the
  supported compilers.
- A distribution package was rejected because none exists: Ubuntu 26.04's `spike` package is an
  unrelated SPIFFE secrets tool.
- Following the Spike submodule pinned by `riscv-gnu-toolchain` was rejected because it is just
  another master snapshot, chosen without Booley's acceptance gates, that trails the maintained line
  by months and ships no binaries.
- lowRISC's `ibex-cosim` fork was rejected as the general channel because its tags carry an
  Ibex-specific co-simulation API and the newest dates from 2022.
- A Booley-maintained fork with release tags was rejected because it adds a mirror to maintain
  without adding verification beyond the acceptance gates above.
- Tracking unpinned HEAD was rejected because image builds would stop being reproducible.
- Refreshing once per Booley release, in a toolchain-refresh PR before the release branch, was
  replaced by the monthly update PR: bumping dependencies while preparing a release put untested
  changes into the release.
- Test-building every new master commit was rejected: the RISC-V image build is too large to run
  speculatively, and a monthly refresh keeps the image current enough.

## Consequences

The acceptance checks prove that the snapshot builds on Booley's base, passes upstream's unit tests,
executes RV32 code, and loads extensions. They do not check architectural behavior such as CSRs,
privilege, traps, or debug; Booley relies on upstream's pull-request and `Debug Quick Test` CI for that
and does not promise more.

The pin can lag master by about a month; users who need a newer Spike fix sooner can request an
out-of-cycle refresh. Each refresh is a normal reviewed change to
`Dockerfile.riscv`, its build assertion, the supported-tool documentation, and the changelog.

Booley provides upstream Spike only. Ibex's UVM co-simulation, which links against the `ibex-cosim`
fork, is out of scope; projects that need it supply that build themselves.
