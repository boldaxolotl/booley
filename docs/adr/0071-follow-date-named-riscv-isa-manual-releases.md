# Follow date-named RISC-V ISA manual releases

The RISC-V Sandbox Image installs the unprivileged and privileged ISA manuals from the newest
published date-named (`YYYYMMDD`) release of upstream
[`riscv/riscv-isa-manual`](https://github.com/riscv/riscv-isa-manual/releases).
These releases provide the ratified reference content Booley advertises for offline use. The initial
pin for this channel is [`20250508`](https://github.com/riscv/riscv-isa-manual/releases/tag/20250508).

Both volumes are installed as PDF and HTML under `$BOOLEY_RISCV_DOCS` (`/opt/riscv-docs`), with stable
names `riscv-isa-unprivileged.{pdf,html}` and `riscv-isa-privileged.{pdf,html}`. `INDEX.md` names both
volumes and their release. Each download has its own SHA-256 pin; a checksum mismatch fails the
image build. Refreshes change the release argument and all four hashes together, keeping installed
filenames stable. The debug specification and psABI retain their existing independent pins.

The release toolchain currency audit compares `ISA_MANUAL_RELEASE` in `Dockerfile.riscv` with the
newest published, non-draft, non-prerelease tag consisting of exactly eight date digits. Inspect all
release pages: GitHub's `latest` endpoint can point to a per-merge snapshot. Select the greatest date
as of the audit, and keep that baseline for the rest of the Booley release. Equal is **current**;
a newer date-named release that passes acceptance is a **routine update**; one that fails acceptance
is **held**, with a follow-up issue naming the failure. The pin is reviewed once per Booley release;
new per-merge snapshots alone never trigger an update.

Acceptance requires checksum-verified downloads of all four assets, the RISC-V image build, and
the session runtime contract's required document paths. Doctor's offline-document probe must also
agree with that file set. Audit findings record the selected release and any held reason.

---
status: accepted
---

## Considered Options

- Per-merge `riscv-isa-release-<sha>-<date>` snapshots were rejected because they change several times
  a week and can include draft content. Their freshness does not make them ratified releases.
- A fixed pin refreshed only on demand was rejected because release audits would have no currency
  baseline and the offline reference could lag newly ratified content indefinitely.
- Unpinned `latest` downloads were rejected because builds would be irreproducible and could select
  the wrong channel.
- Automated refresh jobs are deferred; each refresh is a normal reviewed change at release time.

## Consequences

The manuals can lag upstream development until the next date-named release. This is intentional:
offline reference currency means currency within the ratified-content channel, not upstream HEAD.
Projects needing draft specifications supply those separately. Existing custom references to the
combined filenames must choose the appropriate volume; neither volume is installed under the old
combined name.
