# OpenROAD 26Q3 channel decision - 2026-08-31

This note resolves the channel question in
[#154](https://github.com/boldaxolotl/booley/issues/154). It identifies an
accessible, immutable upstream artifact for the 26Q3 source revision and
records the integration gates passed before Booley advertised 26Q3 as
supported.

## Decision

Use the official `openroad/ubuntu24.04` OCI image for the exact 26Q3 commit,
addressed by digest rather than by its display tag:

```text
docker.io/openroad/ubuntu24.04@sha256:c34542dd5c3624117e8370cfb3a4f37a40bfce73a25f5cefdad3277c4c46ce8a
```

The selected OCI index is public, active, and was pushed by `openroadci`. Its
only executable platform manifest is Linux/AMD64 at
`sha256:4ee0d1463dd527e73922fb6bf4a2b926781906375014d4b2b6fa275e02fa633e`;
the other manifest is its provenance attestation. Docker Hub reports a
3,054,116,459-byte compressed AMD64 image and the complete index digest above.
These values come from the registry's
[exact tag record](https://hub.docker.com/v2/repositories/openroad/ubuntu24.04/tags/26Q2-2580-ga9147cf3ae),
not a search result or mutable `latest` tag.

This is an immutable binary channel, so do not replace it with the VaultLink
package or a fresh build during the upgrade. A source-build fallback is
documented below, but it is secondary because the published OCI digest already
fixes the binary and dependency bytes.

There is still no public 26Q3 `.deb` replacement. Upstream's prebuilt guide
routes users to Precision Innovations
([guide](https://openroad-flow-scripts.readthedocs.io/en/latest/user/BuildWithPrebuilt.html)),
while Precision's last public GitHub release says subsequent binaries moved to
VaultLink
([26Q1 notice](https://github.com/Precision-Innovations/OpenROAD/releases/tag/26Q1)).
The OCI artifact is therefore the public channel decision; it is not evidence
that the old package URL has returned.

## Exact upstream identity

| Item | Pin |
| --- | --- |
| Quarterly tag | [`26Q3`](https://github.com/The-OpenROAD-Project/OpenROAD/tree/26Q3) |
| Annotated tag object | `38d949e4d21fa08f74b04f89cb3e4c69855d90f7` |
| Peeled commit (the source pin) | `a9147cf3aebe65e058bb3fa89c1f9e524488dbb8` |
| Published image tag | `26Q2-2580-ga9147cf3ae` |
| OCI index digest | `sha256:c34542dd5c3624117e8370cfb3a4f37a40bfce73a25f5cefdad3277c4c46ce8a` |
| Linux/AMD64 manifest digest | `sha256:4ee0d1463dd527e73922fb6bf4a2b926781906375014d4b2b6fa275e02fa633e` |

The image tag says `26Q2-2580` because the image was built at 23:03 UTC on
2026-06-30, before automation created the `26Q3` tag at 00:22 UTC on July 1.
The 26Q3 tag nevertheless points to that same
[`a9147cf3...` commit](https://github.com/The-OpenROAD-Project/OpenROAD/commit/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8).
Upstream's quarterly workflow runs at midnight UTC on the first day of each
quarter and creates an annotated `YYQn` tag at the checked-out revision
([workflow](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/.github/workflows/github-actions-quarterly-tag.yml#L1-L52)).

The OCI attestation independently names
`https://github.com/The-OpenROAD-Project/OpenROAD.git` and revision
`a9147cf3aebe65e058bb3fa89c1f9e524488dbb8`. Recheck both the manifest and the
embedded provenance before changing the Dockerfile:

```shell
git ls-remote https://github.com/The-OpenROAD-Project/OpenROAD.git \
  'refs/tags/26Q3*'
docker buildx imagetools inspect \
  openroad/ubuntu24.04@sha256:c34542dd5c3624117e8370cfb3a4f37a40bfce73a25f5cefdad3277c4c46ce8a
docker buildx imagetools inspect --format '{{ json .Provenance }}' \
  openroad/ubuntu24.04@sha256:c34542dd5c3624117e8370cfb3a4f37a40bfce73a25f5cefdad3277c4c46ce8a
```

The annotated Git tag is not signed, and the attached BuildKit provenance is
not a substitute for a publisher signature. The digest gives immutability
after this trust decision; it does not by itself prove publisher identity.
Retain the registry metadata and provenance output as review evidence, and do
not silently repin if Docker Hub later removes the artifact.

For independent source reconstruction, the 26Q3 commit fixes these recursive
submodules through its
[gitlinks](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/.gitmodules):

| Path | Commit |
| --- | --- |
| `src/sta` | [`8572175ac45c42ce8d3d772f73bbb059786b9c66`](https://github.com/The-OpenROAD-Project/OpenSTA/commit/8572175ac45c42ce8d3d772f73bbb059786b9c66) |
| `third-party/abc` | [`d527cfab4ad731b767ea0a2be2021d920d3afece`](https://github.com/The-OpenROAD-Project/abc/commit/d527cfab4ad731b767ea0a2be2021d920d3afece) |
| `third-party/slang-elab` | [`82effc8d9541be69e1ed3ec44759a4449f5d9247`](https://github.com/povik/yosys-slang/commit/82effc8d9541be69e1ed3ec44759a4449f5d9247) |
| `third-party/slang-elab/third_party/fmt` | [`553ec11ec06fbe0beebfbb45f9dc3c9eabd83d28`](https://github.com/fmtlib/fmt/commit/553ec11ec06fbe0beebfbb45f9dc3c9eabd83d28) |
| `third-party/slang-elab/third_party/slang` | [`f04e81565793c768b747a8fd058f3e7aeceee1b5`](https://github.com/MikePopoloski/slang/commit/f04e81565793c768b747a8fd058f3e7aeceee1b5) |

## What the selected image is

The selected artifact is upstream's `builder` stage, not a small `.deb` or the
Dockerfile's `final` stage. Upstream builds in `/OpenROAD`, leaves the complete
source and build tree in the image, and produces
`/OpenROAD/build/bin/openroad`. Its default user is `user` (UID/GID 9000), its
working directory is `/OpenROAD`, and the binary was compiled with the version
string `26Q2-2580-ga9147cf3ae`. These details follow directly from upstream's
[Dockerfile](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/Dockerfile#L35-L78)
and are also present in the OCI configuration history.

Using the image as Booley's runtime-base parent is the safest first
integration because it retains the exact dependency closure. The Booley stage
must immediately restore `USER root`, must not assume `/usr/bin/openroad`, and
must account for the inherited working directory and UID 9000 user. Copying
only the executable into Booley's current Ubuntu image is not equivalent:
upstream's own final stage keeps the full dependency image and copies only the
binary on top of it
([Dockerfile](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/Dockerfile#L71-L78)).
Any slimming attempt therefore needs an explicit `ldd`-derived runtime-library
inventory and a clean-container smoke test.

## Rebuildable source fallback

If the roughly 3.05 GB compressed builder artifact is unacceptable, reproduce
upstream's final stage instead of invoking today's package repositories:

1. Use the public, digest-pinned dependency image
   `docker.io/openroad/ubuntu24.04-dev@sha256:1cfdeba85a28a0bd2a4fca1a5b357fa7f715838941b87a0eeff1686494b1c1db`.
   Its Linux/AMD64 manifest is
   `sha256:b7c05ac54cceb65de9b06b2aa2d5c2d667c8ae648c9499e08d822f24f6e2de8d`
   ([exact registry record](https://hub.docker.com/v2/repositories/openroad/ubuntu24.04-dev/tags/e845c6)).
2. Fetch commit `a9147cf3aebe65e058bb3fa89c1f9e524488dbb8`, verify `HEAD`, and initialize
   the recursive submodules above.
3. Follow the pinned upstream Dockerfile: configure
   `cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
   -DOPENROAD_VERSION=26Q3`, build in parallel, then start a clean stage from
   the same dependency-image digest and copy `build/bin/openroad` into it
   ([Dockerfile build](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/Dockerfile#L39-L78)).
4. Publish the resulting Booley runtime base by its own immutable OCI digest
   and record that digest as the built artifact checksum.

Upstream officially lists Ubuntu 24.04 for local, prebuilt, and Docker
installation
([supported-OS table](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/docs/index.md#L47-L67)).
Its standalone local instructions are a recursive clone, then
`DependencyInstaller.sh -base`, `DependencyInstaller.sh -common -local`, and
`Build.sh`
([build guide](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/docs/user/Build.md#L1-L16),
[dependency and build commands](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/docs/user/Build.md#L118-L140)).
The pinned installer's Ubuntu 24.04 base packages include the compilers, Tcl,
Qt5, Python 3.12, yaml-cpp, zlib, and other build prerequisites
([installer](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/etc/DependencyInstaller.sh#L1013-L1051));
its common phase builds pinned CMake, Bison, Flex, PCRE, SWIG, Boost, Eigen,
CUDD, CUSP, Lemon, spdlog, and GTest, then installs OR-Tools and Abseil
([common phase](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/etc/DependencyInstaller.sh#L956-L979)).

Do not call those installer commands directly in Booley's release Dockerfile.
They run unversioned `apt-get`, several dependencies are fetched by mutable tag,
some archives are checked only with MD5, and the OR-Tools archive has no
checksum in the script
([versions and checksum helper](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/etc/DependencyInstaller.sh#L45-L81),
[OR-Tools download](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/etc/DependencyInstaller.sh#L770-L845)).
The dependency-image digest is the reproducible replacement for those mutable
network operations. This gives reproducible inputs; upstream does not claim
bit-for-bit reproducible compiler output, so the produced OCI digest still has
to be recorded after the build.

## Licensing and redistribution

OpenROAD's top-level code is BSD-3-Clause. Source redistribution must retain
the copyright, conditions, and disclaimer; binary redistribution must
reproduce them in documentation or other accompanying material; and the
project or contributor names cannot be used for endorsement
([license](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/LICENSE#L1-L29)).

The combined binary also contains OpenSTA. OpenSTA states that its open-source
license is GPLv3, with a separate commercial license available from Parallax
Software
([OpenSTA licensing](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/sta/README.md#L57-L72)).
OpenROAD itself warns at startup that some components have more restrictive
licenses which must be honored
([startup notice](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/Main.cc#L595-L600)).

For Booley's public container distribution, treat the OpenROAD executable as a
GPLv3 object-code distribution unless legal review establishes a different
basis. GPLv3 section 6 requires equivalent access to the machine-readable
Corresponding Source and, for a network download, clear directions next to the
object code that remain valid while the object code is offered
([GPLv3 section 6](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/sta/LICENSE#L245-L286)).
The selected builder image retains the exact source tree, which is helpful but
does not remove Booley's obligation as a redistributor. Preserve the OpenROAD
and OpenSTA licenses, provide the exact source/submodule pins and build scripts
with the image or an equivalent durable source-download path, and inventory
the licenses of the Ubuntu and third-party dependency layers. This paragraph
records engineering constraints, not legal advice.

## Compatibility risks and mitigations

The channel decision exposed five integration risks. The implementation for
#154 addresses each one as follows.

1. **Known area-parser break.** The currently supported OpenROAD prints
   `Design area ... u^2 ...`, matching Booley's `_AREA_RE`
   ([old implementation](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a008522d88b669ac4c985609533cf5a3d2649222/src/rsz/src/Resizer.tcl#L665-L672)).
   26Q3 prints `Design area ... um^2 ...`
   ([26Q3 implementation](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/rsz/src/Resizer.tcl#L403-L410)).
   The parser now accepts both spellings and has regression coverage for the
   26Q3 output, preserving compatibility with old logs.
2. **`remove_buffers` semantics changed.** At the old pin, no arguments create
   an empty instance set
   ([old command](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a008522d88b669ac4c985609533cf5a3d2649222/src/rsz/src/Resizer.tcl#L433-L444)).
   In 26Q3, the same no-argument call expands `get_cells` and can select all
   instances before removing buffer cells
   ([26Q3 command](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/rsz/src/Resizer.tcl#L129-L134),
   [no-pattern `get_cells`](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/sta/sdc/Sdc.tcl#L325-L348)).
   Booley now passes `[get_cells *]` explicitly. That preserves the intended
   whole-flat-netlist selection without relying on a version-dependent
   no-argument default.
3. **The banner is not the quarterly tag text.** The selected binary reports
   `26Q2-2580-ga9147cf3ae`, although its source commit is exactly 26Q3. Version
   assertions and user docs recognize the commit equivalence and independently
   verify an embedded 26Q3 source-file checksum.
4. **The image changes base-image behavior.** It is AMD64-only, changes the
   default user and working directory, and is much larger than the current
   `.deb` path. The Booley layer restores root during assembly, creates and
   returns to the `agent` UID/GID 1000 account, restores `/work`, and rebuilds
   its separately pinned Yosys and simulator toolchain. A clean-container check
   confirmed the expected user, working directory, and tool paths.
5. **The Tcl surface still needs end-to-end execution.** The 26Q3 sources retain
   the arguments Booley uses for floorplanning, placement, parasitic
   estimation, repair, and timing—for example `global_placement` still accepts
   density, padding, and `-skip_io`
   ([command definition](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/gpl/src/replace.tcl#L4-L81)),
   and `repair_timing` retains the setup/hold and repair-control flags
   ([command definition](https://github.com/The-OpenROAD-Project/OpenROAD/blob/a9147cf3aebe65e058bb3fa89c1f9e524488dbb8/src/rsz/src/Resizer.tcl#L234-L273)).
   Source compatibility is not behavioral compatibility; the offline
   before/after flow comparison below exercises this Tcl surface with the
   setup-repair pass both enabled and disabled.

## Integration validation

The stable base built successfully from the pinned parent. The local BuildKit
artifact used by the candidate image was
`sha256:cbf947b9bfa2ae99a9a33b59d572d58d927436aad2b60b317003249dc25256fd`.
Its clean-container checks confirmed:

- `openroad -version` normalizes to `26Q2-2580-ga9147cf3ae`;
- `/OpenROAD/src/rsz/src/Resizer.tcl` has SHA-256
  `c8bb060f372392663871afb62ca922f9da1fd58a1b635324da1ec713a88c928f`;
- `/OpenROAD`, `/OpenROAD/LICENSE`, `/OpenROAD/src/sta/LICENSE`, and the Booley
  source-direction notice are present; and
- the runtime starts as `agent` (UID/GID 1000) in `/work`, with OpenROAD,
  Yosys, Icarus, and Verilator on `PATH`.

The production candidate and the published `booley-sandbox:0.2.9` image were
then run against the same tiny counter, pinned Nangate45 inputs, and PPA recipe
with container networking disabled:

| Run | Published image (`v2.0-17598-ga008522d8`) | 26Q3 candidate |
| --- | ---: | ---: |
| Logical mapped area / cells | 31.122 um^2 / 11 | 31.122 um^2 / 11 |
| Physical area / utilization / cells | 31 um^2 / 52% / 11 | 31 um^2 / 52% / 11 |
| Setup WNS / hold WHS | +19.769154 ns / -0.181505 ns | +19.768459 ns / -0.181521 ns |
| Critical path / reg-to-reg Fmax | 230.846 ps / 4331.892 MHz | 231.541 ps / 4318.890 MHz |
| Repair enabled | placement, repair, STA complete | placement, repair, STA complete |
| Repair disabled | placement, STA complete | placement, STA complete |

Both candidates produced non-empty, byte-identical Yosys and OpenROAD output
netlists for this fixture. Their SHA-256 values were respectively
`2c477e26cc0170be55afd50667acb5f07eba410fb451cde18004fbd4fabb9a9a` and
`b73efbe4d16456056e5d6bf1078117b70dd273a5df47c3f9372afa272d132876`.
The 26Q3 log printed `Design area 31 um^2 52% utilization.`, and the updated
parser retained that evidence in the structured report. The hold warning is
the same expected result on both images and does not fail the fixture's default
timing policy.

These gates, the bundled notices, and the durable Corresponding Source path
allow the supported-version line to move from `2.0-17598-ga008522d8` to the
26Q3 commit identity.
