# Ticket Mode production-image smoke plan

## Scope

Extend the existing required production-image CI job with a deterministic, image-level Ticket Mode smoke. Keep full host-issued Session Runtime provisioning and live LLM behavior out of this test boundary.

## Implementation

- [x] Add a dedicated four-bit counter Project with Verible, Icarus, and Yosys/OpenROAD Targets plus a real clock SDC.
- [x] Patch only the Codex backend call in the test and drive the real Developer Agent launch environment through one stdio MCP session.
- [x] Force heavy Flows into detached Jobs and poll each Job to a terminal `EXIT_CODE`.
- [x] Cover one successful Ticket with mandatory Criteria, an unmet optional Criterion, Run Report rejection, TB-dependent Criterion invalidation, re-verification, Scope commit, and review handoff.
- [x] Cover one intentionally unmet mandatory Simulation Criterion, blocked transition, and scripted blocked-triage dossier.
- [x] Fetch the pinned Nangate45 files through the production helper on every CI run, validate checksums, and mount the cache read-only at `/opt/pdk`.
- [x] Assert real OpenROAD timing/PPA data and non-skipped `cell_count_max` and `clk_i.fmax_mhz_min` checks.
- [x] Preserve a literal installed `booley run --dry-run` packaging guard and otherwise replace only the outer child-process launch so the in-process test double survives.

## Verification

- `ruff check src/ tests/`
- Criteria and ticket validation unit tests
- Ordinary host collection, where the image-only module skips
- Fresh-wheel production image build
- Two-Ticket Docker smoke with the PDK mounted read-only
