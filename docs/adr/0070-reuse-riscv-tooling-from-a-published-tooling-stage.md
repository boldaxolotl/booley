# Reuse RISC-V tooling from a published tooling stage

The RISC-V image's toolchain, Spike, and offline specifications are built in a separate
`riscv-tooling` stage of `Dockerfile.riscv`. Its identity is the **tooling key**: a hash of that
stage's exact text, a key-schema version, and the platform. The stage starts `FROM` the same
digest-pinned Ubuntu image as the final stage of `Dockerfile.base`, declares every `ARG` it uses
inside the stage, and copies nothing from the build context. It must not depend on the Booley
runtime base, the standard substrate, or any other application image. The key therefore covers
every input Booley pins. Mutable apt state is not covered; see Consequences. Spike's `configure` disables
its optional Boost support explicitly, so packages the builder happens to carry cannot change which
Spike the key names.

The final stage stays `FROM booley-standard-substrate`. It installs the RISC-V runtime helper
packages (`srecord`, `device-tree-compiler`, `poppler-utils`), copies `/opt/riscv` and
`/opt/riscv-docs` from `riscv-tooling`, and sets the RISC-V environment. The image therefore keeps
its layer-prefix contract over the exact standard substrate, and every candidate still runs the full
RISC-V runtime contract, image-size check, and PicoRV32 demo.

A trusted publisher workflow builds `--target riscv-tooling` only from `main` and stores it in GHCR as
`booley-sandbox-base:riscv-tooling-<key>`. It pushes the candidate by digest only, verifies that
digest, and then creates the final tag, confirming that the tag serves the verified Linux/AMD64
image manifest. It never replaces an existing final tag. Publisher runs serialize per key, so a
queued publication of one key never displaces another key's. The `Tests` workflow resolves that tag to a digest,
checks its role and key labels, and passes the digest as a named build context that overrides the
`riscv-tooling` stage. CI builds the stage itself in three cases: the tag is absent (for example, in
a pull request that changes the stage), the registry cannot be reached, or the composed candidate
fails against the published tooling: either its build (whose final stage runs the tooling's
executables) fails or it fails a quick shared-library and executable check. The last case is
recorded as a compatibility fallback. Pull requests never push. Release publication and local builds
keep building the stage from source.

The `bwave-smoke` duration budget depends on the tooling source. Registry hits get a budget derived
from measured hit runs, and local builds get a separate budget derived from measured local runs. A
slow hit therefore cannot hide behind the cold ceiling, and a correct cold build does not fail a
budget sized for hits.

This supersedes the measurement-only warm path from #568 Phase 1, which saved a BuildKit cache in
GitHub Actions under a key that included the standard parent's layer content.

---
status: accepted
---

## Considered Options

- **Restoring BuildKit cache through GitHub Actions** was rejected. Entries can be evicted (the
  repository already uses about 55 GB of cache). They are saved only when the whole job succeeds, so a
  cache-miss seed that exceeds the `bwave-smoke` duration budget never saves. Entries are
  branch-scoped. The release workflow had already declined this route because the image is larger
  than the original cache budget.
- **Keying on the standard parent's content** was rejected because the decision sample showed that
  the parent is not reproducible across runs. Three runs at one SHA produced three parent
  fingerprints.
- **Building the stage `FROM` the published runtime base and keying it on the stable-base contract**
  was rejected for its hit rate. In the 30 days to 2026-10-04, 28 of the 67 merged pull requests that
  ran the RISC-V lane also changed a stable-base input, usually `pyproject.toml`. Those pull requests
  build the base locally, so their contract is never published and their tooling would always miss.
- **A separate `Dockerfile.riscv-tooling`** was rejected. The Session image lifecycle fingerprints
  `Dockerfile.riscv` as the RISC-V substrate recipe, and the legacy initializer, `build-riscv.sh`, and
  the release workflow each build it with one named context. One file keeps tooling changes inside
  that fingerprint and leaves those callers unchanged.
- **Making the RISC-V image derive `FROM` the tooling image** was rejected because it breaks the
  RISC-V image's layer-prefix contract over the exact standard substrate.
- **Having release publication consume the published tooling image** was deferred. It would save a
  tooling build per release, but it would change release provenance, and the release path is not on
  the pull-request critical path.

## Consequences

Tooling reuse no longer depends on stable-base churn. The key changes only when the stage text
changes, which includes Spike refreshes under ADR 0069, xPack bumps, specification bumps, and the
shared Ubuntu digest.

The tooling binaries are compiled against the Ubuntu packages that existed when the tooling image
was built, not against the runtime base selected by each candidate. Two guards keep that safe. A
static test requires the stage's Ubuntu digest to equal the runtime base's final-stage digest. A
runtime-contract probe requires every ELF file under `/opt/riscv` to resolve its shared libraries in
the candidate. Most remaining skew (a newer `libstdc++` symbol in the tooling than the base provides)
also fails the existing Spike and multilib probes. Apt packages in both images stay unpinned, so the
key does not capture apt drift: whichever apt state the first publication sees becomes the artifact
for that key. The stable-base contract accepts the same limitation. Here it has three mitigations.
The publisher records the builder's `libc6`, `libstdc++6`, `libgcc-s1`, and compiler package
versions on the published image, and it runs the session runtime contract's own RISC-V probes
against the bare tooling image before promotion. A composed candidate that fails to build or fails the quick compatibility check falls back to a
local build instead of failing the pull request. Editing the stage, even only a comment, produces a
new key, which forces a fresh publication.

GHCR tags are mutable. The rule against replacing a final tag binds only the publisher workflow,
which serializes its runs. The registry does not enforce it, and any principal with package write
access could retag. Consumers therefore use only a digest whose labels match the key they computed.
The publisher has the same trust boundary as the stable-base publisher. Same-repository
collaborators who could edit a workflow to raise its token permissions are already trusted with the
stable base.

The restructure changes the `Dockerfile.riscv` recipe fingerprint, so existing local RISC-V
substrates rebuild once. Local and release builds also install the stage's build dependencies on
the bare Ubuntu image, which adds a few minutes to a build that is already uncached.

Expected savings are the measured warm-cache delta (about 780 s of tooling build and 550 s of
required-gate time on the containerd arms) minus the unmeasured cost of pulling and copying a
roughly 2 GB tooling layer on the classic image store that automatic CI uses. #568 claims sustained
savings only after at least 20 comparable automatic runs.
