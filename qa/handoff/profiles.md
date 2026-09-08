# Central profile/check crosswalk

This is a planning extraction, not executed qualification. Sources are the accepted
journey catalogues, [inventory](inventory.md), [submodule companion](submodules.md),
and [Qualification](../QUALIFICATION.md). The named lists below are review aids
containing **exact full IDs**. Production `profiles.yaml` expands the explicitly
named lists into concrete per-run arrays; it supports neither wildcard selection
nor automatic prerequisite inference. Select all listed checks once in scenario
order. Shared observations carry multiple check results where oracles differ;
equivalent IDs in the equivalence table are removed from selections entirely.

## Required runs and exact list composition

| Run | Required profile | Exact named lists selected |
|---|---|---|
| picorv32 / Ubuntu 24.04 x86-64 / Codex | Core Ubuntu Codex | `picorv32-journey-common`, `picorv32-supplement-common`, `picorv32-journey-linux`, `picorv32-supplement-linux` |
| picorv32 / native Windows x86-64 Docker Desktop WSL2 / Codex | Core Windows Codex | `picorv32-journey-common`, `picorv32-supplement-common`, `picorv32-supplement-windows` |
| picorv32 / each of the preceding platform/provider runs | GUI/client integration | Same-run complete Core lists above plus `picorv32-journey-gui`, `picorv32-supplement-gui` |
| taxi / Ubuntu 24.04 x86-64 / Codex | Core Ubuntu Codex | `taxi-journey-common`, `taxi-supplement-common`, `taxi-journey-linux`, `taxi-supplement-linux`, `taxi-submodule-companion` |
| taxi / native Windows x86-64 Docker Desktop WSL2 / Codex | Core Windows Codex | `taxi-journey-common`, `taxi-supplement-common`, `taxi-supplement-windows`, `taxi-submodule-companion` |
| taxi / each of the preceding platform/provider runs | GUI/client integration | Same-run complete Core lists above plus `taxi-journey-gui`, `taxi-supplement-gui` |
| uart / Ubuntu 24.04 x86-64 / Codex | Core Ubuntu Codex | `uart-journey-common`, `uart-supplement-common`, `uart-journey-linux`, `uart-supplement-linux` |
| uart / native Windows x86-64 Docker Desktop WSL2 / Codex | Core Windows Codex | `uart-journey-common`, `uart-supplement-common`, `uart-supplement-windows` |
| uart / each of the preceding platform/provider runs | GUI/client integration | Same-run complete Core lists above plus `uart-journey-gui`, `uart-supplement-gui` |
| PicoRV32 / Ubuntu 24.04 x86-64 / Claude | Required representative compatibility | `claude-ubuntu-core`: complete PicoRV32 journey common and Linux checks, with both original Ticket contracts; no supplemental stress lists |
| PicoRV32 / Ubuntu 24.04 x86-64 / Claude | Required supported-client compatibility | Same-run `claude-ubuntu-core` plus `claude-supported-client`, executed through the actual supported Claude VS Code client |
| PicoRV32 / native Windows x86-64 Docker Desktop WSL2 / Claude | Optional future compatibility | `claude-windows-core` plus `claude-supported-client`; full journey common checks and native Windows foundation, excluding the explicit PicoRV32 journey Linux list; unexecuted until scheduled |

Linux selection never disappears because Vivado is unavailable: its required
profile becomes incomplete. Windows excludes the **exact IDs** in each
`journey-linux` and `supplement-linux` list; these lists are exclusions for platform
inapplicability, not failed or passed checks. Windows-only LF checks are the exact
`supplement-windows` list and are excluded on Ubuntu. GUI/client work is separately
selected, never converted into shell evidence. A combined Core+GUI run executes
its shared producing work once and retains separate profile verdicts.

Each selected check's setup, action and recovery come from its owning catalogue.
Full same-run core selection supplies released package, Project, runtime, Target,
trace, Ticket, candidate, evaluator, authority and cleanup prerequisites for GUI.
A missing prerequisite blocks its dependents; it does not authorize reuse of a
previous run's passing evidence. Actual supported-client exercises occur while
that run's Interactive stage is live, not after cleanup. Explicit GUI-negative
checks such as WCP unavailable are still performed within a qualified GUI profile
with controlled fixture loss/recovery, not used as evidence that all GUI is available.

## Pre-run capability and authority probes

Every run records exact release/package/import provenance, suite revision,
release-matched docs, image digest/payload provenance, clean pinned Project/IP,
provider/auth class readiness without secrets, runtime and host OS/architecture,
Docker/storage availability, artifact-root capacity, deadline and ownership ledger.
PicoRV32 additionally proves RISC-V image/toolchain and 21 GB free Docker storage
plus artifact headroom. Taxi declares derived dependencies and submodule fixture
sources; UART declares frozen corpus/interface/evaluator digests and isolation plus
external-image digest/provenance. Source checkout execution is disallowed.

Linux PicoRV32 probes administrator-approved Vivado 2025.2, canonical installation,
Grant authority, license profile/controlled relay resources and explicit cleanup
ownership. Paid-seat observation is a separate optional experimental run declaration;
no mandatory check consumes a paid seat without site authority. GUI/client runs
probe actual provider VS Code extension, runtime attachment, MCP integration, live
WCP and qualified screenshot observer. Record exact unavailable capability and
IDs; do not alter reviewed scope afterward. Host-policy/network/notification
fixtures additionally require their explicit disposable roots/topic authority.

## Approved Claude compatibility scope

The maintainer approved full original PicoRV32 Ticket contracts for Claude.
The Ubuntu Claude core list is exactly the union of `picorv32-journey-common`
and `picorv32-journey-linux`, without either provider's duplicate supplemental
host/admin stress lists. This preserves all original simulation, Cycle Count,
review, mutation, physical synthesis and applicable Vivado Criteria, both Ticket
lifecycles, their acceptance/cleanup, the complete Interactive exercise and its
producing work. No reduced replacement Ticket payload is needed or permitted.
Linux Vivado unavailability leaves this required profile incomplete.

The supported-client list is the original actual-client check. Its supporting
work is the full same-run Claude core selection, performed with Claude rather
than borrowing Codex or UART results. Core semantics alone do not establish
Claude VS Code integration. A combined core/client run executes the shared work
once, with separate profile verdicts and the same eight-hour bound.

Optional Windows Claude uses exactly the full PicoRV32 common journey plus the
same actual-client check. Its existing input/authority, Project preparation,
Doctor and cleanup checks carry the native-Windows foundation: record actual
native Windows x86-64, Docker Desktop WSL2 and runtime Linux identities,
release/image readiness, canonical Project paths and successful public init/entry.
These prerequisites must be observed in that run; another platform's evidence
cannot substitute. Detailed line-ending and host stress fixtures remain in their
assigned Codex qualification runs and are not added to this optional compatibility
run. The explicit `picorv32-journey-linux` list is excluded on Windows; ordinary
License Profile CRUD remains common. Optional means unexecuted/pending until run,
not an unresolved design choice or an inferred compatibility pass.

## Implementation tasks, not remaining scope decisions

License Profile create/read/update/delete and owned-profile cleanup are common
host-administration checks on both OSes. Grant attachment, relay/runtime and actual
Vivado execution remain Linux-scoped. The explicit lists below reflect the corrected
catalogue; no Windows CRUD exclusion is retained. Compound supplemental H-09 CRUD
and journey CRUD share operation evidence while retaining separate observations
where the supplemental expectation includes additional list/identity behavior.

Both Codex Taxi runs explicitly select its physical Target, constraints, baseline,
clock coverage, threshold and final/archive checks below. The original logical
slang/Yosys invocation remains independently selected. Current rows total 169
PicoRV32, 226 Taxi and 155 UART journey checks, plus 26 Taxi companion checks.

Fixture recipes for provider failures, host-policy mutations and legacy VS Code
container cases remain implementation work. Their stimuli, classifications and
expected effects are already specified in inventory.md; no new product policy is
needed merely to build them. UART concrete cases are generated against its frozen
register schema and retained in a case manifest. These tasks, evaluator/observer
implementation and timing measurement do not grant evidence or extend phase caps.
Missing fixtures or exceeded caps make the affected required profile incomplete.

## Exact equivalence and allocation mapping

These supplemental IDs represent the same concrete observation as the named retained
checks and are not independently selected. Copy their capability references to the
retained checks. Other supplemental checks stay selected because the journey does
not explicitly preserve their full stimulus/oracle (for example generic clean sim
cannot prove missing XML or parser-rejection behavior).

| Unselected supplemental ID | Retained exact check IDs |
|---|---|
| `opentitan-uart-clean-room-greenfield.inventory.H-02.automatic` | `opentitan-uart-clean-room-greenfield.automatic-bootstrap` |
| `opentitan-uart-clean-room-greenfield.inventory.H-02.pending` | `opentitan-uart-clean-room-greenfield.bootstrap-pending` |
| `opentitan-uart-clean-room-greenfield.inventory.H-02.ready` | `opentitan-uart-clean-room-greenfield.bootstrap-clean` |
| `opentitan-uart-clean-room-greenfield.inventory.H-03.init-repeat` | `opentitan-uart-clean-room-greenfield.init-idempotence` |
| `opentitan-uart-clean-room-greenfield.inventory.H-04.scaffold` | `opentitan-uart-clean-room-greenfield.scaffold-assets` |
| `opentitan-uart-clean-room-greenfield.inventory.H-10.doctor-deep` | `opentitan-uart-clean-room-greenfield.setup-doctor-deep` |
| `opentitan-uart-clean-room-greenfield.inventory.H-10.doctor-plain` | `opentitan-uart-clean-room-greenfield.setup-doctor-plain` |
| `opentitan-uart-clean-room-greenfield.inventory.ST-01.stealth-explicit` | `opentitan-uart-clean-room-greenfield.stealth-disabled` |
| `picorv32-published-demo-continuity.inventory.F-04.fpga-fresh` | `picorv32-published-demo-continuity.vivado.implementation` |
| `picorv32-published-demo-continuity.inventory.H-09.relay-failure` | `picorv32-published-demo-continuity.vivado.relay-fault` |
| `picorv32-published-demo-continuity.inventory.H-09.relay-recovery` | `picorv32-published-demo-continuity.vivado.relay-recovery` |
| `picorv32-published-demo-continuity.inventory.H-10.doctor-deep` | `picorv32-published-demo-continuity.doctor.deep` |
| `picorv32-published-demo-continuity.inventory.H-10.doctor-plain` | `picorv32-published-demo-continuity.doctor.plain` |
| `picorv32-published-demo-continuity.inventory.ST-01.stealth-explicit` | `picorv32-published-demo-continuity.stealth.enabled` |
| `picorv32-published-demo-continuity.inventory.ST-03.sanitize-trailer` | `picorv32-published-demo-continuity.stealth.attribution-removal` |
| `picorv32-published-demo-continuity.inventory.ST-03.sanitize-word` | `picorv32-published-demo-continuity.stealth.commit-sanitation` |
| `taxi-10g-mac-port-evolution.inventory.H-10.doctor-deep` | `taxi-10g-mac-port-evolution.doctor.deep` |
| `taxi-10g-mac-port-evolution.inventory.H-10.doctor-plain` | `taxi-10g-mac-port-evolution.doctor.plain` |
| `taxi-10g-mac-port-evolution.inventory.P-02.target-refresh` | `taxi-10g-mac-port-evolution.target.discovery-refresh` |
| `taxi-10g-mac-port-evolution.inventory.P-07.runtime-offline` | `taxi-10g-mac-port-evolution.setup.no-runtime-install` |
| `taxi-10g-mac-port-evolution.inventory.W-02.alias-missing` | `taxi-10g-mac-port-evolution.bwave.alias-missing` |
| `taxi-10g-mac-port-evolution.inventory.W-02.alias-register` | `taxi-10g-mac-port-evolution.bwave.alias-register` |
| `taxi-10g-mac-port-evolution.inventory.W-02.alias-restart` | `taxi-10g-mac-port-evolution.bwave.alias-persist` |
| `taxi-10g-mac-port-evolution.inventory.W-02.alias-stale` | `taxi-10g-mac-port-evolution.bwave.alias-stale` |
| `taxi-10g-mac-port-evolution.inventory.W-02.marker-create` | `taxi-10g-mac-port-evolution.bwave.marker-create` |
| `taxi-10g-mac-port-evolution.inventory.W-02.marker-delete` | `taxi-10g-mac-port-evolution.bwave.marker-delete` |
| `taxi-10g-mac-port-evolution.inventory.W-02.marker-list` | `taxi-10g-mac-port-evolution.bwave.marker-list` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-diff` | `taxi-10g-mac-port-evolution.bwave.semantic-diff` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-distance` | `taxi-10g-mac-port-evolution.bwave.semantic-distance` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-find` | `taxi-10g-mac-port-evolution.bwave.semantic-find` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-list` | `taxi-10g-mac-port-evolution.bwave.semantic-list` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-sample` | `taxi-10g-mac-port-evolution.bwave.semantic-sample` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-signal` | `taxi-10g-mac-port-evolution.bwave.semantic-signal` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-stats` | `taxi-10g-mac-port-evolution.bwave.semantic-stats` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-stuck` | `taxi-10g-mac-port-evolution.bwave.semantic-stuck` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-value` | `taxi-10g-mac-port-evolution.bwave.semantic-value` |
| `taxi-10g-mac-port-evolution.inventory.W-03.query-wave` | `taxi-10g-mac-port-evolution.bwave.semantic-wave` |
| `taxi-10g-mac-port-evolution.inventory.W-04.async-event` | `taxi-10g-mac-port-evolution.bwave.asynchronous-view` |
| `taxi-10g-mac-port-evolution.inventory.W-04.clock-override` | `taxi-10g-mac-port-evolution.bwave.explicit-clock` |
| `taxi-10g-mac-port-evolution.inventory.W-04.sync-event` | `taxi-10g-mac-port-evolution.bwave.synchronous-view` |
| `picorv32-published-demo-continuity.inventory.H-03.init-check` | `opentitan-uart-clean-room-greenfield.inventory.H-03.init-check` |
| `picorv32-published-demo-continuity.inventory.H-03.init-force` | `opentitan-uart-clean-room-greenfield.inventory.H-03.init-force` |
| `picorv32-published-demo-continuity.inventory.H-03.location-guard` | `opentitan-uart-clean-room-greenfield.inventory.H-03.location-guard` |
| `picorv32-published-demo-continuity.inventory.H-03.seed` | `opentitan-uart-clean-room-greenfield.inventory.H-03.seed` |
| `picorv32-published-demo-continuity.inventory.H-03.skip-credentials` | `opentitan-uart-clean-room-greenfield.inventory.H-03.skip-credentials` |
| `picorv32-published-demo-continuity.inventory.H-06.enter-failure` | `opentitan-uart-clean-room-greenfield.inventory.H-06.enter-failure` |
| `picorv32-published-demo-continuity.inventory.H-06.enter-success` | `opentitan-uart-clean-room-greenfield.inventory.H-06.enter-success` |
| `picorv32-published-demo-continuity.inventory.H-06.interrupt` | `opentitan-uart-clean-room-greenfield.inventory.H-06.interrupt` |
| `picorv32-published-demo-continuity.inventory.H-06.refresh` | `opentitan-uart-clean-room-greenfield.inventory.H-06.refresh` |
| `picorv32-published-demo-continuity.inventory.H-06.refresh-rollback` | `opentitan-uart-clean-room-greenfield.inventory.H-06.refresh-rollback` |
| `picorv32-published-demo-continuity.inventory.H-06.refresh-vscode` | `opentitan-uart-clean-room-greenfield.inventory.H-06.refresh-vscode` |
| `picorv32-published-demo-continuity.inventory.H-06.runtime-down` | `opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-down` |
| `picorv32-published-demo-continuity.inventory.H-06.runtime-retry` | `opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-retry` |
| `picorv32-published-demo-continuity.inventory.H-06.runtime-up` | `opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-up` |
| `taxi-10g-mac-port-evolution.inventory.H-03.init-check` | `opentitan-uart-clean-room-greenfield.inventory.H-03.init-check` |
| `taxi-10g-mac-port-evolution.inventory.H-03.init-force` | `opentitan-uart-clean-room-greenfield.inventory.H-03.init-force` |
| `taxi-10g-mac-port-evolution.inventory.H-03.location-guard` | `opentitan-uart-clean-room-greenfield.inventory.H-03.location-guard` |
| `taxi-10g-mac-port-evolution.inventory.H-03.seed` | `opentitan-uart-clean-room-greenfield.inventory.H-03.seed` |
| `taxi-10g-mac-port-evolution.inventory.H-03.skip-credentials` | `opentitan-uart-clean-room-greenfield.inventory.H-03.skip-credentials` |
| `taxi-10g-mac-port-evolution.inventory.H-06.enter-failure` | `opentitan-uart-clean-room-greenfield.inventory.H-06.enter-failure` |
| `taxi-10g-mac-port-evolution.inventory.H-06.enter-success` | `opentitan-uart-clean-room-greenfield.inventory.H-06.enter-success` |
| `taxi-10g-mac-port-evolution.inventory.H-06.interrupt` | `opentitan-uart-clean-room-greenfield.inventory.H-06.interrupt` |
| `taxi-10g-mac-port-evolution.inventory.H-06.refresh` | `opentitan-uart-clean-room-greenfield.inventory.H-06.refresh` |
| `taxi-10g-mac-port-evolution.inventory.H-06.refresh-rollback` | `opentitan-uart-clean-room-greenfield.inventory.H-06.refresh-rollback` |
| `taxi-10g-mac-port-evolution.inventory.H-06.refresh-vscode` | `opentitan-uart-clean-room-greenfield.inventory.H-06.refresh-vscode` |
| `taxi-10g-mac-port-evolution.inventory.H-06.runtime-down` | `opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-down` |
| `taxi-10g-mac-port-evolution.inventory.H-06.runtime-retry` | `opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-retry` |
| `taxi-10g-mac-port-evolution.inventory.H-06.runtime-up` | `opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-up` |

The 28 cross-scenario H-03/H-06 mappings assign identical detailed host fixtures
once to UART, on both required reference platforms. They transfer the inventory
obligation to UART; they do not reuse another run's evidence or remove a prerequisite
from PicoRV32 or Taxi. All original journey initialization, runtime preparation,
execution, cleanup and actual provider-identity checks remain selected. H-03
idempotent init and provider checks also remain per journey. The precise repeated
force/seed/skip/location cases and supervision/refresh/rollback fixtures now have
one canonical supplemental producer. The actual Claude run still proves its own
provider, Interactive and Ticket behavior; UART evidence cannot supply that claim.

## EDA invocation assignment

The integration IDs in inventory.md are references to evidence requirements, not
extra reruns. The retained producing checks are:

| Integration | Selected producing check(s) |
|---|---|
| Verilator sim | `taxi-10g-mac-port-evolution.baseline.full-module`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| Verilator lint | `picorv32-published-demo-continuity.baseline.verilator-lint`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| Icarus | `picorv32-published-demo-continuity.baseline.icarus-main-core`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| Verible | `taxi-10g-mac-port-evolution.baseline.verible`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| Yosys logical and slang | `taxi-10g-mac-port-evolution.baseline.yosys`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| Yosys physical, sv2v and OpenROAD | `picorv32-published-demo-continuity.baseline.physical-synthesis`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| Vivado 2025.2 | `picorv32-published-demo-continuity.vivado.implementation`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| B-Wave Icarus trace | `picorv32-published-demo-continuity.interactive.bwave-diagnosis`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |
| B-Wave Verilator trace | `taxi-10g-mac-port-evolution.bwave.semantic-value`; retain actual invoked version, input/Target identity and fresh tool artifact, as inventory requires. |

FuseSoC/Edalize resolution remains a distinct evidence assertion at each listed
Flow family's producer: retain resolved qualified Target, top, parameters, source
manifest and recipe. Missing resolution provenance leaves that obligation unmet
although the Flow itself passed. RISC-V toolchain and cocotb remain P-08/P-05.
The approved Taxi physical supplement adds another real OpenROAD invocation; it
must not replace the original logical/slang or Pico physical/sv2v integration.

## Supplemental phase assignment

This table assigns **every selected supplemental check** by its exact owner and
capability. Its IDs are the explicit owner supplement lists below; the count is
an audit aid, not a substitute selector. Equivalent removed IDs execute only with
their retained journey check in that check's existing phase. These assignments
consume the already approved caps in [Budgets](budgets.md), including fixture
creation, observation, bounded fault/recovery and evidence capture. They add no
pre-clock work, extra phases or time allowance. No independent mandatory probe is
assigned to UART's conditional repair reserve.

If a row's lifecycle fault needs a companion fixture, create and dispose that
fixture within its assigned phase; do not alter the accepted hardware Ticket
contract or borrow another phase silently. Runtime teardown/restart probes use
owned fixtures so required main-journey state remains valid. GUI checks execute
at the live Interactive phase where their same-run evidence is produced; H-06
VS Code lifecycle fixtures are prepared in the host phase and exercised there
with the qualified client. Final ownership reconciliation remains mandatory in
Pico's 20-minute, Taxi's 15-minute and UART's 30-minute cleanup reserves, including
resources already disposed during phase-local fault recovery. H-09 profile cleanup
belongs after its own fixture's dependents release; later main-journey licensing
resources retain the ordinary final cleanup checks.

| Owner | Inventory capability | Selected supplemental checks | Charged phase/cap |
|---|---|---:|---|
| picorv32-published-demo-continuity | D-03 | 2 | Interactive Mode, waveform and allocated inventory probes (45 min) |
| picorv32-published-demo-continuity | F-01 | 11 | Ticket 1 and its negative/recovery exercises (60 min) |
| picorv32-published-demo-continuity | F-02 | 9 | Clean demo baseline and provisioned Linux Vivado (60 min) |
| picorv32-published-demo-continuity | F-03 | 8 | Clean demo baseline and provisioned Linux Vivado (60 min) |
| picorv32-published-demo-continuity | F-04 | 7 | Clean demo baseline and provisioned Linux Vivado (60 min) |
| picorv32-published-demo-continuity | H-01 | 2 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | H-03 | 2 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | H-07 | 5 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | H-08 | 6 | Clean demo baseline and provisioned Linux Vivado (60 min) |
| picorv32-published-demo-continuity | H-09 | 4 | Clean demo baseline and provisioned Linux Vivado (60 min) |
| picorv32-published-demo-continuity | H-10 | 1 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | I-01 | 2 | Interactive Mode, waveform and allocated inventory probes (45 min) |
| picorv32-published-demo-continuity | I-05 | 2 | Interactive Mode, waveform and allocated inventory probes (45 min) |
| picorv32-published-demo-continuity | P-04 | 10 | Ticket 1 and its negative/recovery exercises (60 min) |
| picorv32-published-demo-continuity | P-06 | 4 | Ticket 1 and its negative/recovery exercises (60 min) |
| picorv32-published-demo-continuity | P-08 | 2 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | S-01 | 2 | Ticket 2, remaining allocated checks and complete final regression (210 min) |
| picorv32-published-demo-continuity | S-04 | 9 | Ticket 2, remaining allocated checks and complete final regression (210 min) |
| picorv32-published-demo-continuity | S-06 | 7 | Ticket 2, remaining allocated checks and complete final regression (210 min) |
| picorv32-published-demo-continuity | ST-01 | 1 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | ST-02 | 5 | Preparation, host/admin probes and Doctor (50 min) |
| picorv32-published-demo-continuity | ST-03 | 9 | Ticket 2, remaining allocated checks and complete final regression (210 min) |
| picorv32-published-demo-continuity | ST-04 | 8 | Interactive Mode, waveform and allocated inventory probes (45 min) |
| picorv32-published-demo-continuity | T-02 | 2 | Ticket 2, remaining allocated checks and complete final regression (210 min) |
| picorv32-published-demo-continuity | W-01 | 3 | Interactive Mode, waveform and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | D-03 | 2 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | F-02 | 9 | Doctor, Target resolution and clean continuity (60 min) |
| taxi-10g-mac-port-evolution | F-03 | 8 | Doctor, Target resolution and clean continuity (60 min) |
| taxi-10g-mac-port-evolution | H-03 | 2 | Preparation, Project Initialization, derived image and complete Setup (75 min) |
| taxi-10g-mac-port-evolution | H-10 | 1 | Preparation, Project Initialization, derived image and complete Setup (75 min) |
| taxi-10g-mac-port-evolution | I-01 | 2 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | I-02 | 6 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | I-04 | 3 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | I-05 | 2 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | P-01 | 5 | Preparation, Project Initialization, derived image and complete Setup (75 min) |
| taxi-10g-mac-port-evolution | P-02 | 6 | Doctor, Target resolution and clean continuity (60 min) |
| taxi-10g-mac-port-evolution | P-03 | 3 | Doctor, Target resolution and clean continuity (60 min) |
| taxi-10g-mac-port-evolution | P-05 | 7 | Doctor, Target resolution and clean continuity (60 min) |
| taxi-10g-mac-port-evolution | P-07 | 6 | Preparation, Project Initialization, derived image and complete Setup (75 min) |
| taxi-10g-mac-port-evolution | P-09 | 2 | Doctor, Target resolution and clean continuity (60 min) |
| taxi-10g-mac-port-evolution | S-01 | 2 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | S-04 | 2 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | S-05 | 7 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | ST-04 | 7 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | T-01 | 6 | Ticket 1 including the complete 7-of-8 mutation campaign (100 min) |
| taxi-10g-mac-port-evolution | T-03 | 10 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-04 | 5 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-05 | 8 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-06 | 4 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-07 | 7 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-08 | 6 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-09 | 5 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-10 | 6 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | T-11 | 10 | Seed proof and Ticket 2 (100 min) |
| taxi-10g-mac-port-evolution | W-01 | 7 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | W-02 | 5 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | W-03 | 20 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | W-04 | 45 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| taxi-10g-mac-port-evolution | W-05 | 8 | Interactive Mode, FST/B-Wave and allocated inventory probes (45 min) |
| opentitan-uart-clean-room-greenfield | D-01 | 7 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | D-02 | 6 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | D-03 | 2 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | H-01 | 4 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-02 | 3 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-03 | 12 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-04 | 1 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-05 | 6 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-06 | 16 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-10 | 7 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-11 | 3 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-12 | 13 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | H-13 | 2 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | I-01 | 5 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | I-03 | 3 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | I-05 | 2 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | P-10 | 6 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | S-01 | 9 | Feature development and acceptance (180 min) |
| opentitan-uart-clean-room-greenfield | S-02 | 5 | Feature development and acceptance (180 min) |
| opentitan-uart-clean-room-greenfield | S-03 | 4 | Feature development and acceptance (180 min) |
| opentitan-uart-clean-room-greenfield | S-04 | 4 | Feature development and acceptance (180 min) |
| opentitan-uart-clean-room-greenfield | ST-01 | 2 | Preparation/bootstrap (45 min) |
| opentitan-uart-clean-room-greenfield | ST-04 | 7 | Setup/Interactive (45 min) |
| opentitan-uart-clean-room-greenfield | T-02 | 4 | Feature development and acceptance (180 min) |
| opentitan-uart-clean-room-greenfield | T-12 | 6 | Setup/Interactive (45 min) |

The 26 exact submodule checks run in Taxi's 40-minute companion phase; its final
cleanup check additionally participates in final ledger reconciliation. The ten
approved physical-synthesis checks use Taxi clean continuity for recipe/baseline,
Seed proof and Ticket 2 for bound candidate metrics, and complete final regression
for final results; artifact/owned-resource cleanup remains in its explicit reserve.
They do not consume the submodule phase or replace logical synthesis.

This assignment exposes a large workload inside fixed caps; it makes no assertion
that it fits. On deadline exhaustion retain missing checks as blocked/incomplete
and perform cleanup. Timing-based redesign would be a later decision informed by
actual runs, not an implicit waiver in this crosswalk.

## Explicit check lists

Empty lists indicate no check of that class; they convey no qualification credit.

### picorv32-journey-common

```text
picorv32-published-demo-continuity.inputs.project-pin
picorv32-published-demo-continuity.inputs.upstream-pin
picorv32-published-demo-continuity.inputs.release
picorv32-published-demo-continuity.inputs.authority
picorv32-published-demo-continuity.project.separate-repository
picorv32-published-demo-continuity.stealth.enabled
picorv32-published-demo-continuity.stealth.hidden-projection
picorv32-published-demo-continuity.stealth.native-no-copy
picorv32-published-demo-continuity.stealth.native-no-symlink
picorv32-published-demo-continuity.stealth.commit-sanitation
picorv32-published-demo-continuity.stealth.attribution-removal
picorv32-published-demo-continuity.doctor.first-product-exercise
picorv32-published-demo-continuity.doctor.plain
picorv32-published-demo-continuity.doctor.deep
picorv32-published-demo-continuity.doctor.plain-recheck
picorv32-published-demo-continuity.baseline.source-unchanged
picorv32-published-demo-continuity.baseline.firmware
picorv32-published-demo-continuity.baseline.targets
picorv32-published-demo-continuity.baseline.icarus-main-core
picorv32-published-demo-continuity.baseline.icarus-axi
picorv32-published-demo-continuity.baseline.icarus-wishbone
picorv32-published-demo-continuity.baseline.icarus-dhrystone
picorv32-published-demo-continuity.baseline.verilator-lint
picorv32-published-demo-continuity.baseline.physical-synthesis
picorv32-published-demo-continuity.vivado.license-create
picorv32-published-demo-continuity.vivado.license-read
picorv32-published-demo-continuity.vivado.license-update
picorv32-published-demo-continuity.vivado.license-delete
picorv32-published-demo-continuity.bwave.semantic-wave
picorv32-published-demo-continuity.bwave.semantic-find
picorv32-published-demo-continuity.bwave.semantic-sample
picorv32-published-demo-continuity.bwave.semantic-distance
picorv32-published-demo-continuity.bwave.semantic-value
picorv32-published-demo-continuity.bwave.rejection-list
picorv32-published-demo-continuity.bwave.rejection-signal
picorv32-published-demo-continuity.bwave.rejection-diff
picorv32-published-demo-continuity.bwave.rejection-stats
picorv32-published-demo-continuity.bwave.rejection-stuck
picorv32-published-demo-continuity.bwave.defect-preservation
picorv32-published-demo-continuity.interactive.child-context
picorv32-published-demo-continuity.interactive.readiness
picorv32-published-demo-continuity.interactive.wishbone-baseline
picorv32-published-demo-continuity.interactive.inject
picorv32-published-demo-continuity.interactive.reproduce
picorv32-published-demo-continuity.interactive.trace-observability
picorv32-published-demo-continuity.interactive.bwave-diagnosis
picorv32-published-demo-continuity.interactive.repair
picorv32-published-demo-continuity.interactive.rerun-simulation
picorv32-published-demo-continuity.interactive.rerun-lint
picorv32-published-demo-continuity.interactive.local-commit
picorv32-published-demo-continuity.interactive.restore
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.payload
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.board
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.basis
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.payload
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.board
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.basis
picorv32-published-demo-continuity.create.barrier
picorv32-published-demo-continuity.create.authoring-boundary
picorv32-published-demo-continuity.retry.exact-allowance
picorv32-published-demo-continuity.ticket1.scope
picorv32-published-demo-continuity.ticket1.fixed-iterations
picorv32-published-demo-continuity.ticket1.firmware-validation
picorv32-published-demo-continuity.ticket1.success-magic
picorv32-published-demo-continuity.ticket1.cycle-record
picorv32-published-demo-continuity.ticket1.elaboration
picorv32-published-demo-continuity.ticket1.simulation
picorv32-published-demo-continuity.ticket1.cycle-cap
picorv32-published-demo-continuity.ticket1.tb-review
picorv32-published-demo-continuity.ticket1.persistent-target
picorv32-published-demo-continuity.guard.negative-result
picorv32-published-demo-continuity.guard.no-success
picorv32-published-demo-continuity.guard.no-cycles
picorv32-published-demo-continuity.guard.restore-rerun
picorv32-published-demo-continuity.guard.discard
picorv32-published-demo-continuity.refresh.old-basis
picorv32-published-demo-continuity.refresh.provider-surface
picorv32-published-demo-continuity.refresh.queue-transition
picorv32-published-demo-continuity.refresh.authored-drift
picorv32-published-demo-continuity.refresh.incompatible-provider
picorv32-published-demo-continuity.ticket2.scope
picorv32-published-demo-continuity.ticket2.isa-authority
picorv32-published-demo-continuity.ticket2.pcpi-timing
picorv32-published-demo-continuity.ticket2.enable-default
picorv32-published-demo-continuity.ticket2.disabled-trap
picorv32-published-demo-continuity.ticket2.zbb-andn
picorv32-published-demo-continuity.ticket2.zbb-orn
picorv32-published-demo-continuity.ticket2.zbb-xnor
picorv32-published-demo-continuity.ticket2.zbb-clz
picorv32-published-demo-continuity.ticket2.zbb-ctz
picorv32-published-demo-continuity.ticket2.zbb-cpop
picorv32-published-demo-continuity.ticket2.zbb-min
picorv32-published-demo-continuity.ticket2.zbb-minu
picorv32-published-demo-continuity.ticket2.zbb-max
picorv32-published-demo-continuity.ticket2.zbb-maxu
picorv32-published-demo-continuity.ticket2.zbb-sext-b
picorv32-published-demo-continuity.ticket2.zbb-sext-h
picorv32-published-demo-continuity.ticket2.zbb-zext-h
picorv32-published-demo-continuity.ticket2.zbb-rol
picorv32-published-demo-continuity.ticket2.zbb-ror
picorv32-published-demo-continuity.ticket2.zbb-rori
picorv32-published-demo-continuity.ticket2.zbb-orc-b
picorv32-published-demo-continuity.ticket2.zbb-rev8
picorv32-published-demo-continuity.ticket2.enabled-core
picorv32-published-demo-continuity.ticket2.enabled-axi
picorv32-published-demo-continuity.ticket2.enabled-wishbone
picorv32-published-demo-continuity.ticket2.elab-sim_core_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_core_zbb
picorv32-published-demo-continuity.ticket2.elab-sim_wb_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_wb_zbb
picorv32-published-demo-continuity.ticket2.elab-sim_zbb_disabled
picorv32-published-demo-continuity.ticket2.sim-sim_zbb_disabled
picorv32-published-demo-continuity.ticket2.regression-main-core
picorv32-published-demo-continuity.ticket2.regression-axi
picorv32-published-demo-continuity.ticket2.regression-wishbone
picorv32-published-demo-continuity.ticket2.provider-execution
picorv32-published-demo-continuity.ticket2.standalone
picorv32-published-demo-continuity.ticket2.lint
picorv32-published-demo-continuity.ticket2.mutation
picorv32-published-demo-continuity.ticket2.synthesis-cell
picorv32-published-demo-continuity.ticket2.synthesis-timing
picorv32-published-demo-continuity.ticket2.review-bugs
picorv32-published-demo-continuity.ticket2.review-protocol
picorv32-published-demo-continuity.ticket2.review-spec
picorv32-published-demo-continuity.ticket2.review-code_style
picorv32-published-demo-continuity.ticket2.review-optimization
picorv32-published-demo-continuity.ticket2.review-security
picorv32-published-demo-continuity.ticket2.tb-review
picorv32-published-demo-continuity.ticket2.bugs-clean
picorv32-published-demo-continuity.ticket2.ephemeral-retention
picorv32-published-demo-continuity.ticket2.ephemeral-removal
picorv32-published-demo-continuity.ticket1.done
picorv32-published-demo-continuity.ticket1.merge
picorv32-published-demo-continuity.ticket1.workspace-cleanup
picorv32-published-demo-continuity.ticket1.triage-report
picorv32-published-demo-continuity.ticket2.done
picorv32-published-demo-continuity.ticket2.merge
picorv32-published-demo-continuity.ticket2.workspace-cleanup
picorv32-published-demo-continuity.ticket2.triage-report
picorv32-published-demo-continuity.final.combined-regression
picorv32-published-demo-continuity.coverage.criterion-families
picorv32-published-demo-continuity.cleanup.branches
picorv32-published-demo-continuity.cleanup.worktrees
picorv32-published-demo-continuity.cleanup.targets
picorv32-published-demo-continuity.cleanup.projections
picorv32-published-demo-continuity.cleanup.hooks
picorv32-published-demo-continuity.cleanup.runtimes
picorv32-published-demo-continuity.cleanup.processes
picorv32-published-demo-continuity.cleanup.license-profiles
picorv32-published-demo-continuity.cleanup.mounts
picorv32-published-demo-continuity.cleanup.preserve-borrowed
picorv32-published-demo-continuity.cleanup.clean-pins
picorv32-published-demo-continuity.cleanup.remotes
picorv32-published-demo-continuity.evidence.finalize
picorv32-published-demo-continuity.ticket2.elab-sim_axi_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_axi_zbb
```

### picorv32-journey-linux

```text
picorv32-published-demo-continuity.vivado.version
picorv32-published-demo-continuity.vivado.registration
picorv32-published-demo-continuity.vivado.project-grant
picorv32-published-demo-continuity.vivado.readonly-mount
picorv32-published-demo-continuity.vivado.license-attach
picorv32-published-demo-continuity.vivado.relay-fault
picorv32-published-demo-continuity.vivado.relay-recovery
picorv32-published-demo-continuity.vivado.implementation
picorv32-published-demo-continuity.ticket2.fpga
picorv32-published-demo-continuity.cleanup.project-grants
picorv32-published-demo-continuity.cleanup.relays
picorv32-published-demo-continuity.cleanup.installation-registrations
```

### picorv32-journey-gui

```text
picorv32-published-demo-continuity.interactive.supported-client
```

### picorv32-supplement-common

```text
picorv32-published-demo-continuity.inventory.D-03.docs-contract
picorv32-published-demo-continuity.inventory.D-03.docs-recovery
picorv32-published-demo-continuity.inventory.F-01.sim-design-fail
picorv32-published-demo-continuity.inventory.F-01.sim-elab
picorv32-published-demo-continuity.inventory.F-01.sim-elab-args
picorv32-published-demo-continuity.inventory.F-01.sim-elab-persistence
picorv32-published-demo-continuity.inventory.F-01.sim-inconclusive
picorv32-published-demo-continuity.inventory.F-01.sim-infra
picorv32-published-demo-continuity.inventory.F-01.sim-multi
picorv32-published-demo-continuity.inventory.F-01.sim-pass
picorv32-published-demo-continuity.inventory.F-01.sim-recovery
picorv32-published-demo-continuity.inventory.F-01.sim-standalone-fail
picorv32-published-demo-continuity.inventory.F-01.sim-standalone-pass
picorv32-published-demo-continuity.inventory.F-02.lint-clean
picorv32-published-demo-continuity.inventory.F-02.lint-dedupe
picorv32-published-demo-continuity.inventory.F-02.lint-hard
picorv32-published-demo-continuity.inventory.F-02.lint-infra
picorv32-published-demo-continuity.inventory.F-02.lint-native-waiver
picorv32-published-demo-continuity.inventory.F-02.lint-nonfatal-warning
picorv32-published-demo-continuity.inventory.F-02.lint-recovery
picorv32-published-demo-continuity.inventory.F-02.lint-scope
picorv32-published-demo-continuity.inventory.F-02.lint-warning
picorv32-published-demo-continuity.inventory.F-03.synth-advisory
picorv32-published-demo-continuity.inventory.F-03.synth-baseline
picorv32-published-demo-continuity.inventory.F-03.synth-frontend-incompatible
picorv32-published-demo-continuity.inventory.F-03.synth-frontend-missing
picorv32-published-demo-continuity.inventory.F-03.synth-latch
picorv32-published-demo-continuity.inventory.F-03.synth-metrics
picorv32-published-demo-continuity.inventory.F-03.synth-recovery
picorv32-published-demo-continuity.inventory.F-03.synth-threshold
picorv32-published-demo-continuity.inventory.H-01.prerequisite
picorv32-published-demo-continuity.inventory.H-01.prerequisite-recovery
picorv32-published-demo-continuity.inventory.H-03.init-repeat
picorv32-published-demo-continuity.inventory.H-03.provider
picorv32-published-demo-continuity.inventory.H-07.forget-granted
picorv32-published-demo-continuity.inventory.H-07.forget-revoked
picorv32-published-demo-continuity.inventory.H-07.inventory-import
picorv32-published-demo-continuity.inventory.H-07.inventory-missing
picorv32-published-demo-continuity.inventory.H-07.inventory-views
picorv32-published-demo-continuity.inventory.H-09.profile-attach
picorv32-published-demo-continuity.inventory.H-09.profile-cleanup
picorv32-published-demo-continuity.inventory.H-09.profile-crud
picorv32-published-demo-continuity.inventory.H-10.doctor-recovery
picorv32-published-demo-continuity.inventory.I-01.interactive-commit
picorv32-published-demo-continuity.inventory.I-05.catalog-coherence
picorv32-published-demo-continuity.inventory.I-05.public-navigation
picorv32-published-demo-continuity.inventory.P-04.cycle-invalid
picorv32-published-demo-continuity.inventory.P-04.cycle-valid
picorv32-published-demo-continuity.inventory.P-04.sentinel-both
picorv32-published-demo-continuity.inventory.P-04.sentinel-fail
picorv32-published-demo-continuity.inventory.P-04.sentinel-none
picorv32-published-demo-continuity.inventory.P-04.sentinel-pass
picorv32-published-demo-continuity.inventory.P-04.sentinel-recovery
picorv32-published-demo-continuity.inventory.P-04.skip-default
picorv32-published-demo-continuity.inventory.P-04.skip-override
picorv32-published-demo-continuity.inventory.P-04.test-token
picorv32-published-demo-continuity.inventory.P-06.disk-budget
picorv32-published-demo-continuity.inventory.P-06.frozen-clock
picorv32-published-demo-continuity.inventory.P-06.prerun-input
picorv32-published-demo-continuity.inventory.P-06.prerun-recovery
picorv32-published-demo-continuity.inventory.P-08.riscv-firmware
picorv32-published-demo-continuity.inventory.P-08.riscv-tools
picorv32-published-demo-continuity.inventory.S-01.review-optimization
picorv32-published-demo-continuity.inventory.S-01.review-rtl-bugs
picorv32-published-demo-continuity.inventory.S-04.criterion-cycle
picorv32-published-demo-continuity.inventory.S-04.criterion-elab
picorv32-published-demo-continuity.inventory.S-04.criterion-lint
picorv32-published-demo-continuity.inventory.S-04.criterion-review-optimization
picorv32-published-demo-continuity.inventory.S-04.criterion-review-rtl-bugs
picorv32-published-demo-continuity.inventory.S-04.criterion-sim
picorv32-published-demo-continuity.inventory.S-04.criterion-standalone
picorv32-published-demo-continuity.inventory.S-04.criterion-synthesis
picorv32-published-demo-continuity.inventory.S-06.baseline-mismatch
picorv32-published-demo-continuity.inventory.S-06.baseline-missing
picorv32-published-demo-continuity.inventory.S-06.baseline-recovery
picorv32-published-demo-continuity.inventory.S-06.threshold-absolute
picorv32-published-demo-continuity.inventory.S-06.threshold-clock
picorv32-published-demo-continuity.inventory.S-06.threshold-directed
picorv32-published-demo-continuity.inventory.S-06.threshold-relative
picorv32-published-demo-continuity.inventory.ST-01.stealth-history
picorv32-published-demo-continuity.inventory.ST-02.dual-repo
picorv32-published-demo-continuity.inventory.ST-02.fresh-clone
picorv32-published-demo-continuity.inventory.ST-02.native-ignore
picorv32-published-demo-continuity.inventory.ST-02.projected-core
picorv32-published-demo-continuity.inventory.ST-02.projection-refresh
picorv32-published-demo-continuity.inventory.ST-03.body-reject
picorv32-published-demo-continuity.inventory.ST-03.escape-sanitized
picorv32-published-demo-continuity.inventory.ST-03.history-recovery
picorv32-published-demo-continuity.inventory.ST-03.push-guard-escape
picorv32-published-demo-continuity.inventory.ST-03.push-identity-allow
picorv32-published-demo-continuity.inventory.ST-03.push-identity-reject
picorv32-published-demo-continuity.inventory.ST-03.push-path-reject
picorv32-published-demo-continuity.inventory.ST-03.push-symlink-reject
picorv32-published-demo-continuity.inventory.ST-03.subject-reject
picorv32-published-demo-continuity.inventory.ST-04.interrupt-cleanup
picorv32-published-demo-continuity.inventory.ST-04.mount-boundary
picorv32-published-demo-continuity.inventory.ST-04.network-deny
picorv32-published-demo-continuity.inventory.ST-04.origin-push-deny
picorv32-published-demo-continuity.inventory.ST-04.pdk-readonly
picorv32-published-demo-continuity.inventory.ST-04.provider-allow
picorv32-published-demo-continuity.inventory.ST-04.runtime-user
picorv32-published-demo-continuity.inventory.T-02.ticket-type-bug-fix
picorv32-published-demo-continuity.inventory.T-02.ticket-type-refactor
picorv32-published-demo-continuity.inventory.W-01.trace-fifo
picorv32-published-demo-continuity.inventory.W-01.trace-fresh
picorv32-published-demo-continuity.inventory.W-01.trace-vcd-convert
```

### picorv32-supplement-linux

```text
picorv32-published-demo-continuity.inventory.F-04.fpga-authority-fail
picorv32-published-demo-continuity.inventory.F-04.fpga-baseline
picorv32-published-demo-continuity.inventory.F-04.fpga-cache
picorv32-published-demo-continuity.inventory.F-04.fpga-design-fail
picorv32-published-demo-continuity.inventory.F-04.fpga-dry
picorv32-published-demo-continuity.inventory.F-04.fpga-no-cache
picorv32-published-demo-continuity.inventory.F-04.fpga-recovery
picorv32-published-demo-continuity.inventory.H-08.grant
picorv32-published-demo-continuity.inventory.H-08.grant-denied
picorv32-published-demo-continuity.inventory.H-08.grant-recovery
picorv32-published-demo-continuity.inventory.H-08.installation-cleanup
picorv32-published-demo-continuity.inventory.H-08.installation-crud
picorv32-published-demo-continuity.inventory.H-08.mount
picorv32-published-demo-continuity.inventory.H-09.relay-isolation
picorv32-published-demo-continuity.inventory.S-04.criterion-fpga
picorv32-published-demo-continuity.inventory.ST-04.vivado-readonly
```

### picorv32-supplement-windows

(empty)

### picorv32-supplement-gui

```text
picorv32-published-demo-continuity.inventory.I-01.client-session
```

### taxi-journey-common

```text
taxi-10g-mac-port-evolution.inputs.pin
taxi-10g-mac-port-evolution.inputs.no-wrapper
taxi-10g-mac-port-evolution.inputs.symlink
taxi-10g-mac-port-evolution.inputs.release
taxi-10g-mac-port-evolution.inputs.authority
taxi-10g-mac-port-evolution.setup.bootstrap-precondition
taxi-10g-mac-port-evolution.setup.initialize
taxi-10g-mac-port-evolution.setup.delegate-authority
taxi-10g-mac-port-evolution.setup.plan
taxi-10g-mac-port-evolution.setup.source-preservation
taxi-10g-mac-port-evolution.setup.image-provenance
taxi-10g-mac-port-evolution.setup.no-runtime-install
taxi-10g-mac-port-evolution.setup.import-probe
taxi-10g-mac-port-evolution.setup.stealth-disabled
taxi-10g-mac-port-evolution.setup.public-navigation
taxi-10g-mac-port-evolution.setup.image-clean-recovery
taxi-10g-mac-port-evolution.image.package-pytest
taxi-10g-mac-port-evolution.image.package-pytest-xdist
taxi-10g-mac-port-evolution.image.package-pytest-split
taxi-10g-mac-port-evolution.image.package-cocotb
taxi-10g-mac-port-evolution.image.package-cocotb-bus
taxi-10g-mac-port-evolution.image.package-cocotb-test
taxi-10g-mac-port-evolution.image.package-cocotbext-axi
taxi-10g-mac-port-evolution.image.package-cocotbext-eth
taxi-10g-mac-port-evolution.image.package-cocotbext-i2c
taxi-10g-mac-port-evolution.image.package-cocotbext-pcie
taxi-10g-mac-port-evolution.image.package-cocotbext-uart
taxi-10g-mac-port-evolution.image.package-scapy
taxi-10g-mac-port-evolution.target.sim-driver
taxi-10g-mac-port-evolution.target.source-closure
taxi-10g-mac-port-evolution.target.lint-driver
taxi-10g-mac-port-evolution.target.synth-driver
taxi-10g-mac-port-evolution.target.unambiguous
taxi-10g-mac-port-evolution.target.cli-mcp
taxi-10g-mac-port-evolution.target.fully-qualified
taxi-10g-mac-port-evolution.target.discovery-refresh
taxi-10g-mac-port-evolution.target.parameter-data_w
taxi-10g-mac-port-evolution.target.parameter-tx_gbx_if_en
taxi-10g-mac-port-evolution.target.parameter-rx_gbx_if_en
taxi-10g-mac-port-evolution.target.parameter-gbx_cnt
taxi-10g-mac-port-evolution.target.parameter-dic_en
taxi-10g-mac-port-evolution.target.parameter-ptp_ts_en
taxi-10g-mac-port-evolution.target.parameter-ptp_td_en
taxi-10g-mac-port-evolution.target.parameter-ptp_ts_fmt_tod
taxi-10g-mac-port-evolution.target.parameter-ptp_ts_fns_w
taxi-10g-mac-port-evolution.target.parameter-ptp_ts_w
taxi-10g-mac-port-evolution.target.parameter-ptp_td_sdi_pipeline
taxi-10g-mac-port-evolution.target.parameter-tx_tag_w
taxi-10g-mac-port-evolution.target.parameter-pfc_en
taxi-10g-mac-port-evolution.target.parameter-pause_en
taxi-10g-mac-port-evolution.target.parameter-stat_en
taxi-10g-mac-port-evolution.target.parameter-stat_tx_level
taxi-10g-mac-port-evolution.target.parameter-stat_rx_level
taxi-10g-mac-port-evolution.target.parameter-stat_id_base
taxi-10g-mac-port-evolution.target.parameter-stat_update_period
taxi-10g-mac-port-evolution.target.parameter-stat_str_en
taxi-10g-mac-port-evolution.target.parameter-stat_prefix_str
taxi-10g-mac-port-evolution.target.register-rx
taxi-10g-mac-port-evolution.target.register-tx
taxi-10g-mac-port-evolution.target.register-tx-alignment
taxi-10g-mac-port-evolution.target.register-tx-underrun
taxi-10g-mac-port-evolution.target.register-tx-user-error
taxi-10g-mac-port-evolution.target.register-lfc
taxi-10g-mac-port-evolution.target.register-pfc
taxi-10g-mac-port-evolution.doctor.plain
taxi-10g-mac-port-evolution.doctor.deep
taxi-10g-mac-port-evolution.doctor.plain-recheck
taxi-10g-mac-port-evolution.doctor.failure-gate
taxi-10g-mac-port-evolution.baseline.full-module
taxi-10g-mac-port-evolution.baseline.normal-frames
taxi-10g-mac-port-evolution.baseline.jumbo-frames
taxi-10g-mac-port-evolution.baseline.rx-ifg12
taxi-10g-mac-port-evolution.baseline.rx-ifg0
taxi-10g-mac-port-evolution.baseline.tx-ifg12
taxi-10g-mac-port-evolution.baseline.tx-ifg0
taxi-10g-mac-port-evolution.baseline.dic-alignment
taxi-10g-mac-port-evolution.baseline.good-fcs
taxi-10g-mac-port-evolution.baseline.tx-underrun
taxi-10g-mac-port-evolution.baseline.tx-user-error
taxi-10g-mac-port-evolution.baseline.lfc-frame
taxi-10g-mac-port-evolution.baseline.pfc-frame
taxi-10g-mac-port-evolution.baseline.rx-timestamps
taxi-10g-mac-port-evolution.baseline.tx-timestamps
taxi-10g-mac-port-evolution.baseline.verible
taxi-10g-mac-port-evolution.baseline.yosys
taxi-10g-mac-port-evolution.interactive.child-context
taxi-10g-mac-port-evolution.interactive.prompt
taxi-10g-mac-port-evolution.interactive.discovery
taxi-10g-mac-port-evolution.interactive.focused-pfc
taxi-10g-mac-port-evolution.fst.metadata
taxi-10g-mac-port-evolution.bwave.alias-register
taxi-10g-mac-port-evolution.bwave.alias-persist
taxi-10g-mac-port-evolution.bwave.alias-stale
taxi-10g-mac-port-evolution.bwave.alias-missing
taxi-10g-mac-port-evolution.bwave.marker-create
taxi-10g-mac-port-evolution.bwave.marker-list
taxi-10g-mac-port-evolution.bwave.marker-resolve
taxi-10g-mac-port-evolution.bwave.marker-delete
taxi-10g-mac-port-evolution.bwave.semantic-list
taxi-10g-mac-port-evolution.bwave.semantic-signal
taxi-10g-mac-port-evolution.bwave.semantic-wave
taxi-10g-mac-port-evolution.bwave.semantic-value
taxi-10g-mac-port-evolution.bwave.semantic-find
taxi-10g-mac-port-evolution.bwave.semantic-sample
taxi-10g-mac-port-evolution.bwave.semantic-diff
taxi-10g-mac-port-evolution.bwave.semantic-distance
taxi-10g-mac-port-evolution.bwave.semantic-stats
taxi-10g-mac-port-evolution.bwave.semantic-stuck
taxi-10g-mac-port-evolution.bwave.synchronous-view
taxi-10g-mac-port-evolution.bwave.asynchronous-view
taxi-10g-mac-port-evolution.bwave.explicit-clock
taxi-10g-mac-port-evolution.bwave.explicit-reset
taxi-10g-mac-port-evolution.bwave.cycle-token
taxi-10g-mac-port-evolution.bwave.typed-physical-time
taxi-10g-mac-port-evolution.bwave.latency-crosscheck
taxi-10g-mac-port-evolution.interactive.tb-review
taxi-10g-mac-port-evolution.interactive.restore
taxi-10g-mac-port-evolution.create.strengthen-10g-mac-observability.payload
taxi-10g-mac-port-evolution.create.strengthen-10g-mac-observability.basis
taxi-10g-mac-port-evolution.create.repair-pfc-priority-routing.payload
taxi-10g-mac-port-evolution.create.repair-pfc-priority-routing.basis
taxi-10g-mac-port-evolution.create.verification-scope
taxi-10g-mac-port-evolution.retry.exact-allowance
taxi-10g-mac-port-evolution.ticket1.target-definition
taxi-10g-mac-port-evolution.ticket1.bad-fcs-stimulus
taxi-10g-mac-port-evolution.ticket1.bad-fcs-tuser
taxi-10g-mac-port-evolution.ticket1.bad-fcs-indication
taxi-10g-mac-port-evolution.ticket1.bad-fcs-counter
taxi-10g-mac-port-evolution.ticket1.pfc-stimulus
taxi-10g-mac-port-evolution.ticket1.pfc-tx-bitmap
taxi-10g-mac-port-evolution.ticket1.pfc-rx-bitmap
taxi-10g-mac-port-evolution.ticket1.pfc-tx-quanta
taxi-10g-mac-port-evolution.ticket1.pfc-rx-quanta
taxi-10g-mac-port-evolution.ticket1.pfc-counter25
taxi-10g-mac-port-evolution.ticket1.pfc-counter57
taxi-10g-mac-port-evolution.ticket1.underrun-stimulus
taxi-10g-mac-port-evolution.ticket1.underrun-termination
taxi-10g-mac-port-evolution.ticket1.underrun-counter
taxi-10g-mac-port-evolution.ticket1.completion-tag
taxi-10g-mac-port-evolution.ticket1.completion-time
taxi-10g-mac-port-evolution.ticket1.statistics-typing
taxi-10g-mac-port-evolution.ticket1.statistics-namespace
taxi-10g-mac-port-evolution.ticket1.elaboration
taxi-10g-mac-port-evolution.ticket1.simulation
taxi-10g-mac-port-evolution.ticket1.tb-review-clean
taxi-10g-mac-port-evolution.ticket1.persistent-target
taxi-10g-mac-port-evolution.mutation.contract
taxi-10g-mac-port-evolution.mutation.source-isolation
taxi-10g-mac-port-evolution.mutation.steering
taxi-10g-mac-port-evolution.mutation.proposal-lock
taxi-10g-mac-port-evolution.mutation.pristine-baseline
taxi-10g-mac-port-evolution.mutation.isolated-results
taxi-10g-mac-port-evolution.mutation.restoration
taxi-10g-mac-port-evolution.mutation.threshold
taxi-10g-mac-port-evolution.mutation.failure-gate
taxi-10g-mac-port-evolution.seed.clean-baseline
taxi-10g-mac-port-evolution.seed.authorized-diff
taxi-10g-mac-port-evolution.seed.upstream-pass
taxi-10g-mac-port-evolution.seed.oracle-fail
taxi-10g-mac-port-evolution.seed.fst-relationship
taxi-10g-mac-port-evolution.seed.invalid-split-recovery
taxi-10g-mac-port-evolution.seed.hidden-location
taxi-10g-mac-port-evolution.ticket2.basis-dependency
taxi-10g-mac-port-evolution.ticket2.scope
taxi-10g-mac-port-evolution.ticket2.reproduce
taxi-10g-mac-port-evolution.ticket2.diagnose
taxi-10g-mac-port-evolution.ticket2.repair-identity
taxi-10g-mac-port-evolution.ticket2.elab-upstream
taxi-10g-mac-port-evolution.ticket2.elab-observability
taxi-10g-mac-port-evolution.ticket2.sim-upstream
taxi-10g-mac-port-evolution.ticket2.sim-observability
taxi-10g-mac-port-evolution.ticket2.lint
taxi-10g-mac-port-evolution.ticket2.synthesis-cells
taxi-10g-mac-port-evolution.ticket2.review-bugs
taxi-10g-mac-port-evolution.ticket2.review-protocol
taxi-10g-mac-port-evolution.ticket2.review-spec
taxi-10g-mac-port-evolution.ticket2.failure-recovery
taxi-10g-mac-port-evolution.ticket1.done
taxi-10g-mac-port-evolution.ticket1.merge
taxi-10g-mac-port-evolution.ticket1.workspace-cleanup
taxi-10g-mac-port-evolution.ticket1.triage-report
taxi-10g-mac-port-evolution.ticket2.done
taxi-10g-mac-port-evolution.ticket2.merge
taxi-10g-mac-port-evolution.ticket2.workspace-cleanup
taxi-10g-mac-port-evolution.ticket2.triage-report
taxi-10g-mac-port-evolution.final.doctor-plain
taxi-10g-mac-port-evolution.final.doctor-deep
taxi-10g-mac-port-evolution.final.upstream
taxi-10g-mac-port-evolution.final.observability
taxi-10g-mac-port-evolution.final.lint
taxi-10g-mac-port-evolution.final.synthesis
taxi-10g-mac-port-evolution.final.bwave
taxi-10g-mac-port-evolution.final.source-identity
taxi-10g-mac-port-evolution.final.accepted-assets
taxi-10g-mac-port-evolution.archive.before-delete
taxi-10g-mac-port-evolution.archive.git-bundle
taxi-10g-mac-port-evolution.archive.evidence
taxi-10g-mac-port-evolution.cleanup.session-runtimes
taxi-10g-mac-port-evolution.cleanup.processes
taxi-10g-mac-port-evolution.cleanup.worktrees
taxi-10g-mac-port-evolution.cleanup.local-branches
taxi-10g-mac-port-evolution.cleanup.project-inventory-entries
taxi-10g-mac-port-evolution.cleanup.project-derived-images
taxi-10g-mac-port-evolution.cleanup.volumes
taxi-10g-mac-port-evolution.cleanup.mounts
taxi-10g-mac-port-evolution.cleanup.inner-project-repository
taxi-10g-mac-port-evolution.cleanup.disposable-clone
taxi-10g-mac-port-evolution.cleanup.preserve-host
taxi-10g-mac-port-evolution.cleanup.remotes
taxi-10g-mac-port-evolution.cleanup.verdict
taxi-10g-mac-port-evolution.failure.independent-continuation
taxi-10g-mac-port-evolution.deadline.finalize
taxi-10g-mac-port-evolution.physical.target
taxi-10g-mac-port-evolution.physical.constraints
taxi-10g-mac-port-evolution.physical.recipe-identity
taxi-10g-mac-port-evolution.physical.discovery-doctor
taxi-10g-mac-port-evolution.physical.clean-baseline
taxi-10g-mac-port-evolution.physical.clock-coverage
taxi-10g-mac-port-evolution.physical.ticket2-cells
taxi-10g-mac-port-evolution.ticket2.synthesis-path
taxi-10g-mac-port-evolution.physical.final
taxi-10g-mac-port-evolution.physical.archive-cleanup
```

### taxi-journey-linux

(empty)

### taxi-journey-gui

```text
taxi-10g-mac-port-evolution.viewer.scoped-state
taxi-10g-mac-port-evolution.viewer.markers-cursor
taxi-10g-mac-port-evolution.viewer.actual-client-open
taxi-10g-mac-port-evolution.viewer.visual-capture
```

### taxi-supplement-common

```text
taxi-10g-mac-port-evolution.inventory.D-03.docs-contract
taxi-10g-mac-port-evolution.inventory.D-03.docs-recovery
taxi-10g-mac-port-evolution.inventory.F-02.lint-clean
taxi-10g-mac-port-evolution.inventory.F-02.lint-dedupe
taxi-10g-mac-port-evolution.inventory.F-02.lint-hard
taxi-10g-mac-port-evolution.inventory.F-02.lint-infra
taxi-10g-mac-port-evolution.inventory.F-02.lint-native-waiver
taxi-10g-mac-port-evolution.inventory.F-02.lint-nonfatal-warning
taxi-10g-mac-port-evolution.inventory.F-02.lint-recovery
taxi-10g-mac-port-evolution.inventory.F-02.lint-scope
taxi-10g-mac-port-evolution.inventory.F-02.lint-warning
taxi-10g-mac-port-evolution.inventory.F-03.synth-advisory
taxi-10g-mac-port-evolution.inventory.F-03.synth-baseline
taxi-10g-mac-port-evolution.inventory.F-03.synth-frontend-incompatible
taxi-10g-mac-port-evolution.inventory.F-03.synth-frontend-missing
taxi-10g-mac-port-evolution.inventory.F-03.synth-latch
taxi-10g-mac-port-evolution.inventory.F-03.synth-metrics
taxi-10g-mac-port-evolution.inventory.F-03.synth-recovery
taxi-10g-mac-port-evolution.inventory.F-03.synth-threshold
taxi-10g-mac-port-evolution.inventory.H-03.init-repeat
taxi-10g-mac-port-evolution.inventory.H-03.provider
taxi-10g-mac-port-evolution.inventory.H-10.doctor-recovery
taxi-10g-mac-port-evolution.inventory.I-01.interactive-commit
taxi-10g-mac-port-evolution.inventory.I-02.cancel-queued
taxi-10g-mac-port-evolution.inventory.I-02.cancel-recovery
taxi-10g-mac-port-evolution.inventory.I-02.cancel-running
taxi-10g-mac-port-evolution.inventory.I-02.detached-poll
taxi-10g-mac-port-evolution.inventory.I-02.report-timeout
taxi-10g-mac-port-evolution.inventory.I-04.interactive-concurrency
taxi-10g-mac-port-evolution.inventory.I-04.interactive-priority
taxi-10g-mac-port-evolution.inventory.I-04.shared-tree-collision
taxi-10g-mac-port-evolution.inventory.I-05.catalog-coherence
taxi-10g-mac-port-evolution.inventory.I-05.public-navigation
taxi-10g-mac-port-evolution.inventory.P-01.setup-conformance
taxi-10g-mac-port-evolution.inventory.P-01.setup-findings
taxi-10g-mac-port-evolution.inventory.P-01.setup-native
taxi-10g-mac-port-evolution.inventory.P-01.setup-plan
taxi-10g-mac-port-evolution.inventory.P-01.setup-selector-docs
taxi-10g-mac-port-evolution.inventory.P-02.target-ambiguous
taxi-10g-mac-port-evolution.inventory.P-02.target-override
taxi-10g-mac-port-evolution.inventory.P-02.target-parameters
taxi-10g-mac-port-evolution.inventory.P-02.target-qualified
taxi-10g-mac-port-evolution.inventory.P-02.target-recovery
taxi-10g-mac-port-evolution.inventory.P-02.target-views
taxi-10g-mac-port-evolution.inventory.P-03.doctor-selection
taxi-10g-mac-port-evolution.inventory.P-03.selftest-hidden
taxi-10g-mac-port-evolution.inventory.P-03.target-axes
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-assertion
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-focused
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-full
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-missing-xml
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-presentation
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-recovery
taxi-10g-mac-port-evolution.inventory.P-05.cocotb-truncated-xml
taxi-10g-mac-port-evolution.inventory.P-07.dependency-refresh
taxi-10g-mac-port-evolution.inventory.P-07.derived-image
taxi-10g-mac-port-evolution.inventory.P-07.hook-failure
taxi-10g-mac-port-evolution.inventory.P-07.hook-once
taxi-10g-mac-port-evolution.inventory.P-07.hook-recovery
taxi-10g-mac-port-evolution.inventory.P-07.host-skill-readonly
taxi-10g-mac-port-evolution.inventory.P-09.many-cores
taxi-10g-mac-port-evolution.inventory.P-09.vendored-ignore
taxi-10g-mac-port-evolution.inventory.S-01.review-code-style
taxi-10g-mac-port-evolution.inventory.S-01.review-protocol
taxi-10g-mac-port-evolution.inventory.S-04.criterion-review-code-style
taxi-10g-mac-port-evolution.inventory.S-04.criterion-review-protocol
taxi-10g-mac-port-evolution.inventory.S-05.diagnostic-call
taxi-10g-mac-port-evolution.inventory.S-05.evidence-rerun
taxi-10g-mac-port-evolution.inventory.S-05.evidence-stale
taxi-10g-mac-port-evolution.inventory.S-05.fail-fix-pass
taxi-10g-mac-port-evolution.inventory.S-05.mandatory-block
taxi-10g-mac-port-evolution.inventory.S-05.optional-justify
taxi-10g-mac-port-evolution.inventory.S-05.out-contract
taxi-10g-mac-port-evolution.inventory.ST-04.interrupt-cleanup
taxi-10g-mac-port-evolution.inventory.ST-04.mount-boundary
taxi-10g-mac-port-evolution.inventory.ST-04.network-deny
taxi-10g-mac-port-evolution.inventory.ST-04.origin-push-deny
taxi-10g-mac-port-evolution.inventory.ST-04.pdk-readonly
taxi-10g-mac-port-evolution.inventory.ST-04.provider-allow
taxi-10g-mac-port-evolution.inventory.ST-04.runtime-user
taxi-10g-mac-port-evolution.inventory.T-01.ticket-draft
taxi-10g-mac-port-evolution.inventory.T-01.ticket-guidance
taxi-10g-mac-port-evolution.inventory.T-01.ticket-invalid-criterion
taxi-10g-mac-port-evolution.inventory.T-01.ticket-invalid-scope
taxi-10g-mac-port-evolution.inventory.T-01.ticket-invalid-target
taxi-10g-mac-port-evolution.inventory.T-01.ticket-seal
taxi-10g-mac-port-evolution.inventory.T-03.dependency-release
taxi-10g-mac-port-evolution.inventory.T-03.dependency-wait
taxi-10g-mac-port-evolution.inventory.T-03.done-shortcut
taxi-10g-mac-port-evolution.inventory.T-03.human-block
taxi-10g-mac-port-evolution.inventory.T-03.human-unblock
taxi-10g-mac-port-evolution.inventory.T-03.resume-workspace
taxi-10g-mac-port-evolution.inventory.T-03.transition-done
taxi-10g-mac-port-evolution.inventory.T-03.transition-queue
taxi-10g-mac-port-evolution.inventory.T-03.transition-review
taxi-10g-mac-port-evolution.inventory.T-03.transition-run
taxi-10g-mac-port-evolution.inventory.T-04.review-queue-invalid
taxi-10g-mac-port-evolution.inventory.T-04.triage-approve
taxi-10g-mac-port-evolution.inventory.T-04.triage-archive
taxi-10g-mac-port-evolution.inventory.T-04.triage-discover
taxi-10g-mac-port-evolution.inventory.T-04.triage-reset
taxi-10g-mac-port-evolution.inventory.T-05.bookkeeping-reject
taxi-10g-mac-port-evolution.inventory.T-05.report-dirty-deleted
taxi-10g-mac-port-evolution.inventory.T-05.report-dirty-modified
taxi-10g-mac-port-evolution.inventory.T-05.report-dirty-staged
taxi-10g-mac-port-evolution.inventory.T-05.report-dirty-untracked
taxi-10g-mac-port-evolution.inventory.T-05.scope-deviation
taxi-10g-mac-port-evolution.inventory.T-05.scope-justification
taxi-10g-mac-port-evolution.inventory.T-05.scope-recovery
taxi-10g-mac-port-evolution.inventory.T-06.basis-edit-reject
taxi-10g-mac-port-evolution.inventory.T-06.basis-pin
taxi-10g-mac-port-evolution.inventory.T-06.basis-redraft
taxi-10g-mac-port-evolution.inventory.T-06.target-disposition
taxi-10g-mac-port-evolution.inventory.T-07.briefing-html
taxi-10g-mac-port-evolution.inventory.T-07.briefing-html-failure
taxi-10g-mac-port-evolution.inventory.T-07.briefing-json
taxi-10g-mac-port-evolution.inventory.T-07.briefing-regenerate
taxi-10g-mac-port-evolution.inventory.T-07.run-result
taxi-10g-mac-port-evolution.inventory.T-07.success-done
taxi-10g-mac-port-evolution.inventory.T-07.success-review
taxi-10g-mac-port-evolution.inventory.T-08.checkpoint-interrupt
taxi-10g-mac-port-evolution.inventory.T-08.checkpoint-resume
taxi-10g-mac-port-evolution.inventory.T-08.developer-timeout
taxi-10g-mac-port-evolution.inventory.T-08.known-transient-retry
taxi-10g-mac-port-evolution.inventory.T-08.ordinary-crash
taxi-10g-mac-port-evolution.inventory.T-08.subscription-requeue
taxi-10g-mac-port-evolution.inventory.T-09.artifact-isolation
taxi-10g-mac-port-evolution.inventory.T-09.atomic-claim
taxi-10g-mac-port-evolution.inventory.T-09.class-cap
taxi-10g-mac-port-evolution.inventory.T-09.queue-cancel
taxi-10g-mac-port-evolution.inventory.T-09.queue-full
taxi-10g-mac-port-evolution.inventory.T-10.board-human
taxi-10g-mac-port-evolution.inventory.T-10.board-machine
taxi-10g-mac-port-evolution.inventory.T-10.check-ready
taxi-10g-mac-port-evolution.inventory.T-10.console
taxi-10g-mac-port-evolution.inventory.T-10.dry-run
taxi-10g-mac-port-evolution.inventory.T-10.idle-drained
taxi-10g-mac-port-evolution.inventory.T-11.human-policy
taxi-10g-mac-port-evolution.inventory.T-11.limit-invalid
taxi-10g-mac-port-evolution.inventory.T-11.model-invalid-role
taxi-10g-mac-port-evolution.inventory.T-11.model-role
taxi-10g-mac-port-evolution.inventory.T-11.model-tier
taxi-10g-mac-port-evolution.inventory.T-11.policy-recovery
taxi-10g-mac-port-evolution.inventory.T-11.report-policy-optional
taxi-10g-mac-port-evolution.inventory.T-11.report-policy-required
taxi-10g-mac-port-evolution.inventory.T-11.timeout-active
taxi-10g-mac-port-evolution.inventory.T-11.timeout-wall
taxi-10g-mac-port-evolution.inventory.W-01.trace-empty
taxi-10g-mac-port-evolution.inventory.W-01.trace-fresh
taxi-10g-mac-port-evolution.inventory.W-01.trace-legacy
taxi-10g-mac-port-evolution.inventory.W-01.trace-native
taxi-10g-mac-port-evolution.inventory.W-01.trace-raw-vcd
taxi-10g-mac-port-evolution.inventory.W-01.trace-recovery
taxi-10g-mac-port-evolution.inventory.W-01.trace-truncated
taxi-10g-mac-port-evolution.inventory.W-02.alias-replace
taxi-10g-mac-port-evolution.inventory.W-02.marker-wrapper-diff
taxi-10g-mac-port-evolution.inventory.W-02.marker-wrapper-value
taxi-10g-mac-port-evolution.inventory.W-02.marker-wrapper-wave
taxi-10g-mac-port-evolution.inventory.W-02.trace-same-path
taxi-10g-mac-port-evolution.inventory.W-03.docs-list
taxi-10g-mac-port-evolution.inventory.W-03.docs-search
taxi-10g-mac-port-evolution.inventory.W-03.docs-show
taxi-10g-mac-port-evolution.inventory.W-03.json-diff-rejected
taxi-10g-mac-port-evolution.inventory.W-03.json-distance-rejected
taxi-10g-mac-port-evolution.inventory.W-03.json-find-supported
taxi-10g-mac-port-evolution.inventory.W-03.json-list-supported
taxi-10g-mac-port-evolution.inventory.W-03.json-sample-rejected
taxi-10g-mac-port-evolution.inventory.W-03.json-signal-rejected
taxi-10g-mac-port-evolution.inventory.W-03.json-stats-supported
taxi-10g-mac-port-evolution.inventory.W-03.json-stuck-rejected
taxi-10g-mac-port-evolution.inventory.W-03.json-value-supported
taxi-10g-mac-port-evolution.inventory.W-03.json-wave-rejected
taxi-10g-mac-port-evolution.inventory.W-03.query-ambiguous
taxi-10g-mac-port-evolution.inventory.W-03.query-limit
taxi-10g-mac-port-evolution.inventory.W-03.query-pattern-miss
taxi-10g-mac-port-evolution.inventory.W-03.schema
taxi-10g-mac-port-evolution.inventory.W-03.skill
taxi-10g-mac-port-evolution.inventory.W-03.version
taxi-10g-mac-port-evolution.inventory.W-04.async-bare-time
taxi-10g-mac-port-evolution.inventory.W-04.bad-literal
taxi-10g-mac-port-evolution.inventory.W-04.literal-slice
taxi-10g-mac-port-evolution.inventory.W-04.marker-build-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-diff-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-distance-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-docs-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-find-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-list-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-sample-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-schema-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-signal-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-skill-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-stats-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-stuck-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-value-rejected
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-async-bare
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-async-column
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-negative
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-outside
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-render
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-repeat
taxi-10g-mac-port-evolution.inventory.W-04.marker-wave-shared-time
taxi-10g-mac-port-evolution.inventory.W-04.query-input-recovery
taxi-10g-mac-port-evolution.inventory.W-04.reset-include
taxi-10g-mac-port-evolution.inventory.W-04.reset-skip
taxi-10g-mac-port-evolution.inventory.W-04.time-units
taxi-10g-mac-port-evolution.inventory.W-04.unknown-signal
taxi-10g-mac-port-evolution.inventory.W-04.virtual-build-rejected
taxi-10g-mac-port-evolution.inventory.W-04.virtual-diff-rejected
taxi-10g-mac-port-evolution.inventory.W-04.virtual-distance-malformed
taxi-10g-mac-port-evolution.inventory.W-04.virtual-distance-semantic
taxi-10g-mac-port-evolution.inventory.W-04.virtual-find-malformed
taxi-10g-mac-port-evolution.inventory.W-04.virtual-find-semantic
taxi-10g-mac-port-evolution.inventory.W-04.virtual-list-rejected
taxi-10g-mac-port-evolution.inventory.W-04.virtual-sample-malformed
taxi-10g-mac-port-evolution.inventory.W-04.virtual-sample-semantic
taxi-10g-mac-port-evolution.inventory.W-04.virtual-signal-rejected
taxi-10g-mac-port-evolution.inventory.W-04.virtual-stats-rejected
taxi-10g-mac-port-evolution.inventory.W-04.virtual-stuck-rejected
taxi-10g-mac-port-evolution.inventory.W-04.virtual-value-malformed
taxi-10g-mac-port-evolution.inventory.W-04.virtual-value-semantic
taxi-10g-mac-port-evolution.inventory.W-04.virtual-wave-malformed
taxi-10g-mac-port-evolution.inventory.W-04.virtual-wave-semantic
taxi-10g-mac-port-evolution.inventory.W-04.width-mismatch
```

### taxi-supplement-linux

(empty)

### taxi-supplement-windows

(empty)

### taxi-supplement-gui

```text
taxi-10g-mac-port-evolution.inventory.I-01.client-session
taxi-10g-mac-port-evolution.inventory.I-02.mcp-schema
taxi-10g-mac-port-evolution.inventory.W-03.legacy-translation
taxi-10g-mac-port-evolution.inventory.W-05.gui-append
taxi-10g-mac-port-evolution.inventory.W-05.gui-bare-fallback
taxi-10g-mac-port-evolution.inventory.W-05.gui-cap
taxi-10g-mac-port-evolution.inventory.W-05.gui-dropped
taxi-10g-mac-port-evolution.inventory.W-05.gui-no-clock
taxi-10g-mac-port-evolution.inventory.W-05.gui-no-wcp
taxi-10g-mac-port-evolution.inventory.W-05.gui-recovery
taxi-10g-mac-port-evolution.inventory.W-05.gui-scoped
```

### uart-journey-common

```text
opentitan-uart-clean-room-greenfield.published-release
opentitan-uart-clean-room-greenfield.run-inputs
opentitan-uart-clean-room-greenfield.delegation
opentitan-uart-clean-room-greenfield.authority
opentitan-uart-clean-room-greenfield.empty-origin
opentitan-uart-clean-room-greenfield.corpus-pin
opentitan-uart-clean-room-greenfield.source-boundary
opentitan-uart-clean-room-greenfield.doc-precedence
opentitan-uart-clean-room-greenfield.doc-rx-depth
opentitan-uart-clean-room-greenfield.doc-tx-overflow
opentitan-uart-clean-room-greenfield.doc-watermark-shift
opentitan-uart-clean-room-greenfield.interface-only
opentitan-uart-clean-room-greenfield.bootstrap-pending
opentitan-uart-clean-room-greenfield.automatic-bootstrap
opentitan-uart-clean-room-greenfield.bootstrap-clean
opentitan-uart-clean-room-greenfield.init-idempotence
opentitan-uart-clean-room-greenfield.scaffold-assets
opentitan-uart-clean-room-greenfield.stealth-disabled
opentitan-uart-clean-room-greenfield.setup-new
opentitan-uart-clean-room-greenfield.setup-doctor-plain
opentitan-uart-clean-room-greenfield.setup-doctor-deep
opentitan-uart-clean-room-greenfield.setup-doctor-final
opentitan-uart-clean-room-greenfield.interactive-prompt
opentitan-uart-clean-room-greenfield.interactive-edit
opentitan-uart-clean-room-greenfield.interactive-mcp
opentitan-uart-clean-room-greenfield.interactive-sim
opentitan-uart-clean-room-greenfield.interactive-lint
opentitan-uart-clean-room-greenfield.interactive-synth
opentitan-uart-clean-room-greenfield.interactive-doctor
opentitan-uart-clean-room-greenfield.interactive-trace
opentitan-uart-clean-room-greenfield.interactive-bwave
opentitan-uart-clean-room-greenfield.interactive-commit
opentitan-uart-clean-room-greenfield.feature-create
opentitan-uart-clean-room-greenfield.feature-run
opentitan-uart-clean-room-greenfield.feature-acceptance-basis
opentitan-uart-clean-room-greenfield.feature-elaboration
opentitan-uart-clean-room-greenfield.feature-simulation
opentitan-uart-clean-room-greenfield.feature-lint
opentitan-uart-clean-room-greenfield.feature-synthesis
opentitan-uart-clean-room-greenfield.feature-review-rtl
opentitan-uart-clean-room-greenfield.feature-review-protocol
opentitan-uart-clean-room-greenfield.feature-review-spec
opentitan-uart-clean-room-greenfield.feature-review-tb
opentitan-uart-clean-room-greenfield.feature-disposition
opentitan-uart-clean-room-greenfield.repair-trigger
opentitan-uart-clean-room-greenfield.repair-create
opentitan-uart-clean-room-greenfield.repair-diagnostics
opentitan-uart-clean-room-greenfield.repair-regression
opentitan-uart-clean-room-greenfield.repair-full-rerun
opentitan-uart-clean-room-greenfield.repair-bounds
opentitan-uart-clean-room-greenfield.evaluator-isolation
opentitan-uart-clean-room-greenfield.evaluator-identity
opentitan-uart-clean-room-greenfield.evaluator-candidate
opentitan-uart-clean-room-greenfield.control-positive-mmio
opentitan-uart-clean-room-greenfield.control-negative-mmio
opentitan-uart-clean-room-greenfield.control-recover-mmio
opentitan-uart-clean-room-greenfield.control-positive-serial
opentitan-uart-clean-room-greenfield.control-negative-serial
opentitan-uart-clean-room-greenfield.control-recover-serial
opentitan-uart-clean-room-greenfield.case-manifest
opentitan-uart-clean-room-greenfield.evaluator-results
opentitan-uart-clean-room-greenfield.evaluator-reuse
opentitan-uart-clean-room-greenfield.UART-BUS.request-stable
opentitan-uart-clean-room-greenfield.UART-BUS.response-stable
opentitan-uart-clean-room-greenfield.UART-BUS.single-outstanding
opentitan-uart-clean-room-greenfield.UART-BUS.accept-latency
opentitan-uart-clean-room-greenfield.UART-BUS.response-latency
opentitan-uart-clean-room-greenfield.UART-BUS.read-snapshot
opentitan-uart-clean-room-greenfield.UART-BUS.read-once
opentitan-uart-clean-room-greenfield.UART-BUS.write-once
opentitan-uart-clean-room-greenfield.UART-BUS.byte-masks
opentitan-uart-clean-room-greenfield.UART-BUS.w1c-masks
opentitan-uart-clean-room-greenfield.UART-BUS.wdata-strobe
opentitan-uart-clean-room-greenfield.UART-BUS.zero-strobe
opentitan-uart-clean-room-greenfield.UART-BUS.read-strobes
opentitan-uart-clean-room-greenfield.UART-BUS.ro-write
opentitan-uart-clean-room-greenfield.UART-BUS.wo-read
opentitan-uart-clean-room-greenfield.UART-BUS.misaligned
opentitan-uart-clean-room-greenfield.UART-BUS.unmapped
opentitan-uart-clean-room-greenfield.UART-BUS.empty-read
opentitan-uart-clean-room-greenfield.UART-REG.offsets
opentitan-uart-clean-room-greenfield.UART-REG.reset-values
opentitan-uart-clean-room-greenfield.UART-REG.legal-rw
opentitan-uart-clean-room-greenfield.UART-REG.reserved
opentitan-uart-clean-room-greenfield.UART-REG.alert-test
opentitan-uart-clean-room-greenfield.UART-TXRX.tx-bytes
opentitan-uart-clean-room-greenfield.UART-TXRX.rx-bytes
opentitan-uart-clean-room-greenfield.UART-TXRX.back-to-back
opentitan-uart-clean-room-greenfield.UART-TXRX.full-duplex
opentitan-uart-clean-room-greenfield.UART-TXRX.enable-disable
opentitan-uart-clean-room-greenfield.UART-BAUD.exact-a
opentitan-uart-clean-room-greenfield.UART-BAUD.exact-b
opentitan-uart-clean-room-greenfield.UART-BAUD.fractional
opentitan-uart-clean-room-greenfield.UART-BAUD.zero
opentitan-uart-clean-room-greenfield.UART-PARITY.disabled
opentitan-uart-clean-room-greenfield.UART-PARITY.even
opentitan-uart-clean-room-greenfield.UART-PARITY.odd
opentitan-uart-clean-room-greenfield.UART-PARITY.bad
opentitan-uart-clean-room-greenfield.UART-FIFO.tx-occupancy
opentitan-uart-clean-room-greenfield.UART-FIFO.rx-occupancy
opentitan-uart-clean-room-greenfield.UART-FIFO.overflow
opentitan-uart-clean-room-greenfield.UART-FIFO.order
opentitan-uart-clean-room-greenfield.UART-FIFO.tx-reset
opentitan-uart-clean-room-greenfield.UART-FIFO.rx-reset
opentitan-uart-clean-room-greenfield.UART-WATERMARK.tx
opentitan-uart-clean-room-greenfield.UART-WATERMARK.rx
opentitan-uart-clean-room-greenfield.UART-IRQ.identities
opentitan-uart-clean-room-greenfield.UART-IRQ.enable-mask
opentitan-uart-clean-room-greenfield.UART-IRQ.test
opentitan-uart-clean-room-greenfield.UART-IRQ.level
opentitan-uart-clean-room-greenfield.UART-IRQ.event
opentitan-uart-clean-room-greenfield.UART-IRQ.tx-empty-done
opentitan-uart-clean-room-greenfield.UART-ERROR.stop
opentitan-uart-clean-room-greenfield.UART-ERROR.break
opentitan-uart-clean-room-greenfield.UART-ERROR.timeout-enabled
opentitan-uart-clean-room-greenfield.UART-ERROR.timeout-disabled
opentitan-uart-clean-room-greenfield.UART-FILTER.noise
opentitan-uart-clean-room-greenfield.UART-FILTER.false-start
opentitan-uart-clean-room-greenfield.UART-FILTER.phase-start
opentitan-uart-clean-room-greenfield.UART-LOOP.system
opentitan-uart-clean-room-greenfield.UART-LOOP.line
opentitan-uart-clean-room-greenfield.UART-OVERRIDE.low
opentitan-uart-clean-room-greenfield.UART-OVERRIDE.high
opentitan-uart-clean-room-greenfield.UART-OVERRIDE.release
opentitan-uart-clean-room-greenfield.UART-HISTORY.order
opentitan-uart-clean-room-greenfield.UART-RESET.idle
opentitan-uart-clean-room-greenfield.UART-RESET.active-tx
opentitan-uart-clean-room-greenfield.UART-RESET.active-rx
opentitan-uart-clean-room-greenfield.UART-RESET.occupied
opentitan-uart-clean-room-greenfield.UART-RESET.pending-mmio
opentitan-uart-clean-room-greenfield.external-prerequisite
opentitan-uart-clean-room-greenfield.external-project
opentitan-uart-clean-room-greenfield.external-release
opentitan-uart-clean-room-greenfield.external-doctor
opentitan-uart-clean-room-greenfield.external-smoke-first
opentitan-uart-clean-room-greenfield.external-recreate
opentitan-uart-clean-room-greenfield.external-smoke-second
opentitan-uart-clean-room-greenfield.external-cleanup
opentitan-uart-clean-room-greenfield.final-simulation
opentitan-uart-clean-room-greenfield.final-lint
opentitan-uart-clean-room-greenfield.final-synthesis
opentitan-uart-clean-room-greenfield.final-doctor
opentitan-uart-clean-room-greenfield.final-git-stealth
opentitan-uart-clean-room-greenfield.archive
opentitan-uart-clean-room-greenfield.cleanup-processes
opentitan-uart-clean-room-greenfield.cleanup-git
opentitan-uart-clean-room-greenfield.cleanup-projects
opentitan-uart-clean-room-greenfield.cleanup-evaluator
opentitan-uart-clean-room-greenfield.cleanup-preservation
opentitan-uart-clean-room-greenfield.deadline
opentitan-uart-clean-room-greenfield.retry-policy
opentitan-uart-clean-room-greenfield.interruption
```

### uart-journey-linux

(empty)

### uart-journey-gui

```text
opentitan-uart-clean-room-greenfield.gui-runtime-client
opentitan-uart-clean-room-greenfield.gui-mcp-integration
opentitan-uart-clean-room-greenfield.gui-waveform
```

### uart-supplement-common

```text
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-atomic
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-concurrent
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-filed
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-kinds
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-list
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-regenerate
opentitan-uart-clean-room-greenfield.inventory.D-01.feedback-triage
opentitan-uart-clean-room-greenfield.inventory.D-02.feedback-block-submit
opentitan-uart-clean-room-greenfield.inventory.D-02.feedback-export
opentitan-uart-clean-room-greenfield.inventory.D-02.feedback-file-only
opentitan-uart-clean-room-greenfield.inventory.D-02.feedback-off
opentitan-uart-clean-room-greenfield.inventory.D-02.feedback-redact
opentitan-uart-clean-room-greenfield.inventory.D-02.feedback-token
opentitan-uart-clean-room-greenfield.inventory.D-03.docs-contract
opentitan-uart-clean-room-greenfield.inventory.D-03.docs-recovery
opentitan-uart-clean-room-greenfield.inventory.H-01.booley-rtl
opentitan-uart-clean-room-greenfield.inventory.H-01.install
opentitan-uart-clean-room-greenfield.inventory.H-01.pip-distro
opentitan-uart-clean-room-greenfield.inventory.H-01.pip-recovery
opentitan-uart-clean-room-greenfield.inventory.H-02.force-repair
opentitan-uart-clean-room-greenfield.inventory.H-02.idempotent
opentitan-uart-clean-room-greenfield.inventory.H-02.repair-ready
opentitan-uart-clean-room-greenfield.inventory.H-03.init-check
opentitan-uart-clean-room-greenfield.inventory.H-03.init-force
opentitan-uart-clean-room-greenfield.inventory.H-03.location-guard
opentitan-uart-clean-room-greenfield.inventory.H-03.provider
opentitan-uart-clean-room-greenfield.inventory.H-03.seed
opentitan-uart-clean-room-greenfield.inventory.H-03.skip-credentials
opentitan-uart-clean-room-greenfield.inventory.H-04.scaffold-repeat
opentitan-uart-clean-room-greenfield.inventory.H-05.auth-clear
opentitan-uart-clean-room-greenfield.inventory.H-05.auth-precedence
opentitan-uart-clean-room-greenfield.inventory.H-05.auth-recovery
opentitan-uart-clean-room-greenfield.inventory.H-05.auth-secret-boundary
opentitan-uart-clean-room-greenfield.inventory.H-05.auth-stdin
opentitan-uart-clean-room-greenfield.inventory.H-05.auth-store
opentitan-uart-clean-room-greenfield.inventory.H-06.enter-failure
opentitan-uart-clean-room-greenfield.inventory.H-06.enter-success
opentitan-uart-clean-room-greenfield.inventory.H-06.interrupt
opentitan-uart-clean-room-greenfield.inventory.H-06.refresh
opentitan-uart-clean-room-greenfield.inventory.H-06.refresh-rollback
opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-down
opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-retry
opentitan-uart-clean-room-greenfield.inventory.H-06.runtime-up
opentitan-uart-clean-room-greenfield.inventory.H-10.doctor-recovery
opentitan-uart-clean-room-greenfield.inventory.H-10.doctor-warning
opentitan-uart-clean-room-greenfield.inventory.H-10.failure-not-waivable
opentitan-uart-clean-room-greenfield.inventory.H-10.health-stale
opentitan-uart-clean-room-greenfield.inventory.H-10.waiver-expired
opentitan-uart-clean-room-greenfield.inventory.H-10.waiver-stale
opentitan-uart-clean-room-greenfield.inventory.H-10.waiver-valid
opentitan-uart-clean-room-greenfield.inventory.H-11.upgrade-ack
opentitan-uart-clean-room-greenfield.inventory.H-11.upgrade-heal
opentitan-uart-clean-room-greenfield.inventory.H-11.upgrade-pending
opentitan-uart-clean-room-greenfield.inventory.H-12.config-cap
opentitan-uart-clean-room-greenfield.inventory.H-12.config-defaults
opentitan-uart-clean-room-greenfield.inventory.H-12.config-denied-egress
opentitan-uart-clean-room-greenfield.inventory.H-12.config-egress
opentitan-uart-clean-room-greenfield.inventory.H-12.config-idle
opentitan-uart-clean-room-greenfield.inventory.H-12.config-invalid-ip
opentitan-uart-clean-room-greenfield.inventory.H-12.config-invalid-key
opentitan-uart-clean-room-greenfield.inventory.H-12.config-invalid-path
opentitan-uart-clean-room-greenfield.inventory.H-12.config-invalid-port
opentitan-uart-clean-room-greenfield.inventory.H-12.config-invalid-scheme
opentitan-uart-clean-room-greenfield.inventory.H-12.config-invalid-wildcard
opentitan-uart-clean-room-greenfield.inventory.H-12.config-preserve
opentitan-uart-clean-room-greenfield.inventory.H-12.config-recovery
opentitan-uart-clean-room-greenfield.inventory.H-13.license-locality
opentitan-uart-clean-room-greenfield.inventory.H-13.route-policy
opentitan-uart-clean-room-greenfield.inventory.I-01.bare-launch
opentitan-uart-clean-room-greenfield.inventory.I-01.bare-launch-failure
opentitan-uart-clean-room-greenfield.inventory.I-01.bare-launch-recovery
opentitan-uart-clean-room-greenfield.inventory.I-01.interactive-commit
opentitan-uart-clean-room-greenfield.inventory.I-03.mcp-disabled
opentitan-uart-clean-room-greenfield.inventory.I-03.mcp-modes
opentitan-uart-clean-room-greenfield.inventory.I-03.mcp-restored
opentitan-uart-clean-room-greenfield.inventory.I-05.catalog-coherence
opentitan-uart-clean-room-greenfield.inventory.I-05.public-navigation
opentitan-uart-clean-room-greenfield.inventory.P-10.custom-criterion
opentitan-uart-clean-room-greenfield.inventory.P-10.custom-disabled
opentitan-uart-clean-room-greenfield.inventory.P-10.custom-discover
opentitan-uart-clean-room-greenfield.inventory.P-10.custom-invalid
opentitan-uart-clean-room-greenfield.inventory.P-10.custom-preflight
opentitan-uart-clean-room-greenfield.inventory.P-10.custom-recovery
opentitan-uart-clean-room-greenfield.inventory.S-01.review-clean-blocked
opentitan-uart-clean-room-greenfield.inventory.S-01.review-clean-waived
opentitan-uart-clean-room-greenfield.inventory.S-01.review-done
opentitan-uart-clean-room-greenfield.inventory.S-01.review-guidance
opentitan-uart-clean-room-greenfield.inventory.S-01.review-rerun
opentitan-uart-clean-room-greenfield.inventory.S-01.review-security
opentitan-uart-clean-room-greenfield.inventory.S-01.review-spec
opentitan-uart-clean-room-greenfield.inventory.S-01.review-stale
opentitan-uart-clean-room-greenfield.inventory.S-01.review-tb-quality
opentitan-uart-clean-room-greenfield.inventory.S-02.mutation-auto-dry
opentitan-uart-clean-room-greenfield.inventory.S-02.mutation-campaign
opentitan-uart-clean-room-greenfield.inventory.S-02.mutation-default-dry
opentitan-uart-clean-room-greenfield.inventory.S-02.mutation-lock-regen
opentitan-uart-clean-room-greenfield.inventory.S-02.mutation-lock-reuse
opentitan-uart-clean-room-greenfield.inventory.S-03.isolation-complete
opentitan-uart-clean-room-greenfield.inventory.S-03.isolation-failure
opentitan-uart-clean-room-greenfield.inventory.S-03.isolation-hidden
opentitan-uart-clean-room-greenfield.inventory.S-03.isolation-visible
opentitan-uart-clean-room-greenfield.inventory.S-04.criterion-mutation
opentitan-uart-clean-room-greenfield.inventory.S-04.criterion-review-security
opentitan-uart-clean-room-greenfield.inventory.S-04.criterion-review-spec
opentitan-uart-clean-room-greenfield.inventory.S-04.criterion-review-tb-quality
opentitan-uart-clean-room-greenfield.inventory.ST-01.nonstealth-history
opentitan-uart-clean-room-greenfield.inventory.ST-01.stealth-legacy
opentitan-uart-clean-room-greenfield.inventory.ST-04.interrupt-cleanup
opentitan-uart-clean-room-greenfield.inventory.ST-04.mount-boundary
opentitan-uart-clean-room-greenfield.inventory.ST-04.network-deny
opentitan-uart-clean-room-greenfield.inventory.ST-04.origin-push-deny
opentitan-uart-clean-room-greenfield.inventory.ST-04.pdk-readonly
opentitan-uart-clean-room-greenfield.inventory.ST-04.provider-allow
opentitan-uart-clean-room-greenfield.inventory.ST-04.runtime-user
opentitan-uart-clean-room-greenfield.inventory.T-02.accepted-commit
opentitan-uart-clean-room-greenfield.inventory.T-02.report-retained
opentitan-uart-clean-room-greenfield.inventory.T-02.ticket-type-feature
opentitan-uart-clean-room-greenfield.inventory.T-02.ticket-type-verification
opentitan-uart-clean-room-greenfield.inventory.T-12.notify-absent
opentitan-uart-clean-room-greenfield.inventory.T-12.notify-blocked
opentitan-uart-clean-room-greenfield.inventory.T-12.notify-completion-drift
opentitan-uart-clean-room-greenfield.inventory.T-12.notify-doctor
opentitan-uart-clean-room-greenfield.inventory.T-12.notify-unavailable-blocked
opentitan-uart-clean-room-greenfield.inventory.T-12.notify-unavailable-doctor
```

### uart-supplement-linux

(empty)

### uart-supplement-windows

```text
opentitan-uart-clean-room-greenfield.inventory.H-03.lf-auto
opentitan-uart-clean-room-greenfield.inventory.H-03.lf-compat
opentitan-uart-clean-room-greenfield.inventory.H-03.lf-dirty
opentitan-uart-clean-room-greenfield.inventory.H-03.lf-hardlink
opentitan-uart-clean-room-greenfield.inventory.H-03.lf-protected
opentitan-uart-clean-room-greenfield.inventory.H-03.lf-recovery
```

### uart-supplement-gui

```text
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-poststop-failure
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-refuse-ambiguous
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-refuse-foreign
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-refuse-headless
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-refuse-multiple
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-replace
opentitan-uart-clean-room-greenfield.inventory.H-06.legacy-restart
opentitan-uart-clean-room-greenfield.inventory.H-06.refresh-vscode
opentitan-uart-clean-room-greenfield.inventory.I-01.client-session
```

### taxi-submodule-companion

```text
taxi-10g-mac-port-evolution.submodules.fixture
taxi-10g-mac-port-evolution.submodules.ticket-current
taxi-10g-mac-port-evolution.submodules.simulation-dependency-baseline
taxi-10g-mac-port-evolution.submodules.simulation-missing-top
taxi-10g-mac-port-evolution.submodules.simulation-restore-top
taxi-10g-mac-port-evolution.submodules.simulation-missing-nested
taxi-10g-mac-port-evolution.submodules.simulation-restore-nested
taxi-10g-mac-port-evolution.submodules.simulation-missing-cold
taxi-10g-mac-port-evolution.submodules.baseline-historical
taxi-10g-mac-port-evolution.submodules.standalone
taxi-10g-mac-port-evolution.submodules.offline
taxi-10g-mac-port-evolution.submodules.default-all
taxi-10g-mac-port-evolution.submodules.select-one
taxi-10g-mac-port-evolution.submodules.select-none
taxi-10g-mac-port-evolution.submodules.simulation-excluded-dependency
taxi-10g-mac-port-evolution.submodules.selection-intersection
taxi-10g-mac-port-evolution.submodules.paired-always
taxi-10g-mac-port-evolution.submodules.missing
taxi-10g-mac-port-evolution.submodules.dirty
taxi-10g-mac-port-evolution.submodules.shallow
taxi-10g-mac-port-evolution.submodules.incomplete-objects
taxi-10g-mac-port-evolution.submodules.rollback
taxi-10g-mac-port-evolution.submodules.matching-destination
taxi-10g-mac-port-evolution.submodules.restore
taxi-10g-mac-port-evolution.submodules.taxi-unchanged
taxi-10g-mac-port-evolution.submodules.cleanup
```

### claude-ubuntu-core

```text
picorv32-published-demo-continuity.inputs.project-pin
picorv32-published-demo-continuity.inputs.upstream-pin
picorv32-published-demo-continuity.inputs.release
picorv32-published-demo-continuity.inputs.authority
picorv32-published-demo-continuity.project.separate-repository
picorv32-published-demo-continuity.stealth.enabled
picorv32-published-demo-continuity.stealth.hidden-projection
picorv32-published-demo-continuity.stealth.native-no-copy
picorv32-published-demo-continuity.stealth.native-no-symlink
picorv32-published-demo-continuity.stealth.commit-sanitation
picorv32-published-demo-continuity.stealth.attribution-removal
picorv32-published-demo-continuity.doctor.first-product-exercise
picorv32-published-demo-continuity.doctor.plain
picorv32-published-demo-continuity.doctor.deep
picorv32-published-demo-continuity.doctor.plain-recheck
picorv32-published-demo-continuity.baseline.source-unchanged
picorv32-published-demo-continuity.baseline.firmware
picorv32-published-demo-continuity.baseline.targets
picorv32-published-demo-continuity.baseline.icarus-main-core
picorv32-published-demo-continuity.baseline.icarus-axi
picorv32-published-demo-continuity.baseline.icarus-wishbone
picorv32-published-demo-continuity.baseline.icarus-dhrystone
picorv32-published-demo-continuity.baseline.verilator-lint
picorv32-published-demo-continuity.baseline.physical-synthesis
picorv32-published-demo-continuity.vivado.license-create
picorv32-published-demo-continuity.vivado.license-read
picorv32-published-demo-continuity.vivado.license-update
picorv32-published-demo-continuity.vivado.license-delete
picorv32-published-demo-continuity.bwave.semantic-wave
picorv32-published-demo-continuity.bwave.semantic-find
picorv32-published-demo-continuity.bwave.semantic-sample
picorv32-published-demo-continuity.bwave.semantic-distance
picorv32-published-demo-continuity.bwave.semantic-value
picorv32-published-demo-continuity.bwave.rejection-list
picorv32-published-demo-continuity.bwave.rejection-signal
picorv32-published-demo-continuity.bwave.rejection-diff
picorv32-published-demo-continuity.bwave.rejection-stats
picorv32-published-demo-continuity.bwave.rejection-stuck
picorv32-published-demo-continuity.bwave.defect-preservation
picorv32-published-demo-continuity.interactive.child-context
picorv32-published-demo-continuity.interactive.readiness
picorv32-published-demo-continuity.interactive.wishbone-baseline
picorv32-published-demo-continuity.interactive.inject
picorv32-published-demo-continuity.interactive.reproduce
picorv32-published-demo-continuity.interactive.trace-observability
picorv32-published-demo-continuity.interactive.bwave-diagnosis
picorv32-published-demo-continuity.interactive.repair
picorv32-published-demo-continuity.interactive.rerun-simulation
picorv32-published-demo-continuity.interactive.rerun-lint
picorv32-published-demo-continuity.interactive.local-commit
picorv32-published-demo-continuity.interactive.restore
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.payload
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.board
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.basis
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.payload
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.board
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.basis
picorv32-published-demo-continuity.create.barrier
picorv32-published-demo-continuity.create.authoring-boundary
picorv32-published-demo-continuity.retry.exact-allowance
picorv32-published-demo-continuity.ticket1.scope
picorv32-published-demo-continuity.ticket1.fixed-iterations
picorv32-published-demo-continuity.ticket1.firmware-validation
picorv32-published-demo-continuity.ticket1.success-magic
picorv32-published-demo-continuity.ticket1.cycle-record
picorv32-published-demo-continuity.ticket1.elaboration
picorv32-published-demo-continuity.ticket1.simulation
picorv32-published-demo-continuity.ticket1.cycle-cap
picorv32-published-demo-continuity.ticket1.tb-review
picorv32-published-demo-continuity.ticket1.persistent-target
picorv32-published-demo-continuity.guard.negative-result
picorv32-published-demo-continuity.guard.no-success
picorv32-published-demo-continuity.guard.no-cycles
picorv32-published-demo-continuity.guard.restore-rerun
picorv32-published-demo-continuity.guard.discard
picorv32-published-demo-continuity.refresh.old-basis
picorv32-published-demo-continuity.refresh.provider-surface
picorv32-published-demo-continuity.refresh.queue-transition
picorv32-published-demo-continuity.refresh.authored-drift
picorv32-published-demo-continuity.refresh.incompatible-provider
picorv32-published-demo-continuity.ticket2.scope
picorv32-published-demo-continuity.ticket2.isa-authority
picorv32-published-demo-continuity.ticket2.pcpi-timing
picorv32-published-demo-continuity.ticket2.enable-default
picorv32-published-demo-continuity.ticket2.disabled-trap
picorv32-published-demo-continuity.ticket2.zbb-andn
picorv32-published-demo-continuity.ticket2.zbb-orn
picorv32-published-demo-continuity.ticket2.zbb-xnor
picorv32-published-demo-continuity.ticket2.zbb-clz
picorv32-published-demo-continuity.ticket2.zbb-ctz
picorv32-published-demo-continuity.ticket2.zbb-cpop
picorv32-published-demo-continuity.ticket2.zbb-min
picorv32-published-demo-continuity.ticket2.zbb-minu
picorv32-published-demo-continuity.ticket2.zbb-max
picorv32-published-demo-continuity.ticket2.zbb-maxu
picorv32-published-demo-continuity.ticket2.zbb-sext-b
picorv32-published-demo-continuity.ticket2.zbb-sext-h
picorv32-published-demo-continuity.ticket2.zbb-zext-h
picorv32-published-demo-continuity.ticket2.zbb-rol
picorv32-published-demo-continuity.ticket2.zbb-ror
picorv32-published-demo-continuity.ticket2.zbb-rori
picorv32-published-demo-continuity.ticket2.zbb-orc-b
picorv32-published-demo-continuity.ticket2.zbb-rev8
picorv32-published-demo-continuity.ticket2.enabled-core
picorv32-published-demo-continuity.ticket2.enabled-axi
picorv32-published-demo-continuity.ticket2.enabled-wishbone
picorv32-published-demo-continuity.ticket2.elab-sim_core_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_core_zbb
picorv32-published-demo-continuity.ticket2.elab-sim_wb_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_wb_zbb
picorv32-published-demo-continuity.ticket2.elab-sim_zbb_disabled
picorv32-published-demo-continuity.ticket2.sim-sim_zbb_disabled
picorv32-published-demo-continuity.ticket2.regression-main-core
picorv32-published-demo-continuity.ticket2.regression-axi
picorv32-published-demo-continuity.ticket2.regression-wishbone
picorv32-published-demo-continuity.ticket2.provider-execution
picorv32-published-demo-continuity.ticket2.standalone
picorv32-published-demo-continuity.ticket2.lint
picorv32-published-demo-continuity.ticket2.mutation
picorv32-published-demo-continuity.ticket2.synthesis-cell
picorv32-published-demo-continuity.ticket2.synthesis-timing
picorv32-published-demo-continuity.ticket2.review-bugs
picorv32-published-demo-continuity.ticket2.review-protocol
picorv32-published-demo-continuity.ticket2.review-spec
picorv32-published-demo-continuity.ticket2.review-code_style
picorv32-published-demo-continuity.ticket2.review-optimization
picorv32-published-demo-continuity.ticket2.review-security
picorv32-published-demo-continuity.ticket2.tb-review
picorv32-published-demo-continuity.ticket2.bugs-clean
picorv32-published-demo-continuity.ticket2.ephemeral-retention
picorv32-published-demo-continuity.ticket2.ephemeral-removal
picorv32-published-demo-continuity.ticket1.done
picorv32-published-demo-continuity.ticket1.merge
picorv32-published-demo-continuity.ticket1.workspace-cleanup
picorv32-published-demo-continuity.ticket1.triage-report
picorv32-published-demo-continuity.ticket2.done
picorv32-published-demo-continuity.ticket2.merge
picorv32-published-demo-continuity.ticket2.workspace-cleanup
picorv32-published-demo-continuity.ticket2.triage-report
picorv32-published-demo-continuity.final.combined-regression
picorv32-published-demo-continuity.coverage.criterion-families
picorv32-published-demo-continuity.cleanup.branches
picorv32-published-demo-continuity.cleanup.worktrees
picorv32-published-demo-continuity.cleanup.targets
picorv32-published-demo-continuity.cleanup.projections
picorv32-published-demo-continuity.cleanup.hooks
picorv32-published-demo-continuity.cleanup.runtimes
picorv32-published-demo-continuity.cleanup.processes
picorv32-published-demo-continuity.cleanup.license-profiles
picorv32-published-demo-continuity.cleanup.mounts
picorv32-published-demo-continuity.cleanup.preserve-borrowed
picorv32-published-demo-continuity.cleanup.clean-pins
picorv32-published-demo-continuity.cleanup.remotes
picorv32-published-demo-continuity.evidence.finalize
picorv32-published-demo-continuity.ticket2.elab-sim_axi_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_axi_zbb
picorv32-published-demo-continuity.vivado.version
picorv32-published-demo-continuity.vivado.registration
picorv32-published-demo-continuity.vivado.project-grant
picorv32-published-demo-continuity.vivado.readonly-mount
picorv32-published-demo-continuity.vivado.license-attach
picorv32-published-demo-continuity.vivado.relay-fault
picorv32-published-demo-continuity.vivado.relay-recovery
picorv32-published-demo-continuity.vivado.implementation
picorv32-published-demo-continuity.ticket2.fpga
picorv32-published-demo-continuity.cleanup.project-grants
picorv32-published-demo-continuity.cleanup.relays
picorv32-published-demo-continuity.cleanup.installation-registrations
```

### claude-windows-core

```text
picorv32-published-demo-continuity.inputs.project-pin
picorv32-published-demo-continuity.inputs.upstream-pin
picorv32-published-demo-continuity.inputs.release
picorv32-published-demo-continuity.inputs.authority
picorv32-published-demo-continuity.project.separate-repository
picorv32-published-demo-continuity.stealth.enabled
picorv32-published-demo-continuity.stealth.hidden-projection
picorv32-published-demo-continuity.stealth.native-no-copy
picorv32-published-demo-continuity.stealth.native-no-symlink
picorv32-published-demo-continuity.stealth.commit-sanitation
picorv32-published-demo-continuity.stealth.attribution-removal
picorv32-published-demo-continuity.doctor.first-product-exercise
picorv32-published-demo-continuity.doctor.plain
picorv32-published-demo-continuity.doctor.deep
picorv32-published-demo-continuity.doctor.plain-recheck
picorv32-published-demo-continuity.baseline.source-unchanged
picorv32-published-demo-continuity.baseline.firmware
picorv32-published-demo-continuity.baseline.targets
picorv32-published-demo-continuity.baseline.icarus-main-core
picorv32-published-demo-continuity.baseline.icarus-axi
picorv32-published-demo-continuity.baseline.icarus-wishbone
picorv32-published-demo-continuity.baseline.icarus-dhrystone
picorv32-published-demo-continuity.baseline.verilator-lint
picorv32-published-demo-continuity.baseline.physical-synthesis
picorv32-published-demo-continuity.vivado.license-create
picorv32-published-demo-continuity.vivado.license-read
picorv32-published-demo-continuity.vivado.license-update
picorv32-published-demo-continuity.vivado.license-delete
picorv32-published-demo-continuity.bwave.semantic-wave
picorv32-published-demo-continuity.bwave.semantic-find
picorv32-published-demo-continuity.bwave.semantic-sample
picorv32-published-demo-continuity.bwave.semantic-distance
picorv32-published-demo-continuity.bwave.semantic-value
picorv32-published-demo-continuity.bwave.rejection-list
picorv32-published-demo-continuity.bwave.rejection-signal
picorv32-published-demo-continuity.bwave.rejection-diff
picorv32-published-demo-continuity.bwave.rejection-stats
picorv32-published-demo-continuity.bwave.rejection-stuck
picorv32-published-demo-continuity.bwave.defect-preservation
picorv32-published-demo-continuity.interactive.child-context
picorv32-published-demo-continuity.interactive.readiness
picorv32-published-demo-continuity.interactive.wishbone-baseline
picorv32-published-demo-continuity.interactive.inject
picorv32-published-demo-continuity.interactive.reproduce
picorv32-published-demo-continuity.interactive.trace-observability
picorv32-published-demo-continuity.interactive.bwave-diagnosis
picorv32-published-demo-continuity.interactive.repair
picorv32-published-demo-continuity.interactive.rerun-simulation
picorv32-published-demo-continuity.interactive.rerun-lint
picorv32-published-demo-continuity.interactive.local-commit
picorv32-published-demo-continuity.interactive.restore
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.payload
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.board
picorv32-published-demo-continuity.create.dhrystone-self-checking-cycle-contract.basis
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.payload
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.board
picorv32-published-demo-continuity.create.rv32-zbb-pcpi.basis
picorv32-published-demo-continuity.create.barrier
picorv32-published-demo-continuity.create.authoring-boundary
picorv32-published-demo-continuity.retry.exact-allowance
picorv32-published-demo-continuity.ticket1.scope
picorv32-published-demo-continuity.ticket1.fixed-iterations
picorv32-published-demo-continuity.ticket1.firmware-validation
picorv32-published-demo-continuity.ticket1.success-magic
picorv32-published-demo-continuity.ticket1.cycle-record
picorv32-published-demo-continuity.ticket1.elaboration
picorv32-published-demo-continuity.ticket1.simulation
picorv32-published-demo-continuity.ticket1.cycle-cap
picorv32-published-demo-continuity.ticket1.tb-review
picorv32-published-demo-continuity.ticket1.persistent-target
picorv32-published-demo-continuity.guard.negative-result
picorv32-published-demo-continuity.guard.no-success
picorv32-published-demo-continuity.guard.no-cycles
picorv32-published-demo-continuity.guard.restore-rerun
picorv32-published-demo-continuity.guard.discard
picorv32-published-demo-continuity.refresh.old-basis
picorv32-published-demo-continuity.refresh.provider-surface
picorv32-published-demo-continuity.refresh.queue-transition
picorv32-published-demo-continuity.refresh.authored-drift
picorv32-published-demo-continuity.refresh.incompatible-provider
picorv32-published-demo-continuity.ticket2.scope
picorv32-published-demo-continuity.ticket2.isa-authority
picorv32-published-demo-continuity.ticket2.pcpi-timing
picorv32-published-demo-continuity.ticket2.enable-default
picorv32-published-demo-continuity.ticket2.disabled-trap
picorv32-published-demo-continuity.ticket2.zbb-andn
picorv32-published-demo-continuity.ticket2.zbb-orn
picorv32-published-demo-continuity.ticket2.zbb-xnor
picorv32-published-demo-continuity.ticket2.zbb-clz
picorv32-published-demo-continuity.ticket2.zbb-ctz
picorv32-published-demo-continuity.ticket2.zbb-cpop
picorv32-published-demo-continuity.ticket2.zbb-min
picorv32-published-demo-continuity.ticket2.zbb-minu
picorv32-published-demo-continuity.ticket2.zbb-max
picorv32-published-demo-continuity.ticket2.zbb-maxu
picorv32-published-demo-continuity.ticket2.zbb-sext-b
picorv32-published-demo-continuity.ticket2.zbb-sext-h
picorv32-published-demo-continuity.ticket2.zbb-zext-h
picorv32-published-demo-continuity.ticket2.zbb-rol
picorv32-published-demo-continuity.ticket2.zbb-ror
picorv32-published-demo-continuity.ticket2.zbb-rori
picorv32-published-demo-continuity.ticket2.zbb-orc-b
picorv32-published-demo-continuity.ticket2.zbb-rev8
picorv32-published-demo-continuity.ticket2.enabled-core
picorv32-published-demo-continuity.ticket2.enabled-axi
picorv32-published-demo-continuity.ticket2.enabled-wishbone
picorv32-published-demo-continuity.ticket2.elab-sim_core_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_core_zbb
picorv32-published-demo-continuity.ticket2.elab-sim_wb_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_wb_zbb
picorv32-published-demo-continuity.ticket2.elab-sim_zbb_disabled
picorv32-published-demo-continuity.ticket2.sim-sim_zbb_disabled
picorv32-published-demo-continuity.ticket2.regression-main-core
picorv32-published-demo-continuity.ticket2.regression-axi
picorv32-published-demo-continuity.ticket2.regression-wishbone
picorv32-published-demo-continuity.ticket2.provider-execution
picorv32-published-demo-continuity.ticket2.standalone
picorv32-published-demo-continuity.ticket2.lint
picorv32-published-demo-continuity.ticket2.mutation
picorv32-published-demo-continuity.ticket2.synthesis-cell
picorv32-published-demo-continuity.ticket2.synthesis-timing
picorv32-published-demo-continuity.ticket2.review-bugs
picorv32-published-demo-continuity.ticket2.review-protocol
picorv32-published-demo-continuity.ticket2.review-spec
picorv32-published-demo-continuity.ticket2.review-code_style
picorv32-published-demo-continuity.ticket2.review-optimization
picorv32-published-demo-continuity.ticket2.review-security
picorv32-published-demo-continuity.ticket2.tb-review
picorv32-published-demo-continuity.ticket2.bugs-clean
picorv32-published-demo-continuity.ticket2.ephemeral-retention
picorv32-published-demo-continuity.ticket2.ephemeral-removal
picorv32-published-demo-continuity.ticket1.done
picorv32-published-demo-continuity.ticket1.merge
picorv32-published-demo-continuity.ticket1.workspace-cleanup
picorv32-published-demo-continuity.ticket1.triage-report
picorv32-published-demo-continuity.ticket2.done
picorv32-published-demo-continuity.ticket2.merge
picorv32-published-demo-continuity.ticket2.workspace-cleanup
picorv32-published-demo-continuity.ticket2.triage-report
picorv32-published-demo-continuity.final.combined-regression
picorv32-published-demo-continuity.coverage.criterion-families
picorv32-published-demo-continuity.cleanup.branches
picorv32-published-demo-continuity.cleanup.worktrees
picorv32-published-demo-continuity.cleanup.targets
picorv32-published-demo-continuity.cleanup.projections
picorv32-published-demo-continuity.cleanup.hooks
picorv32-published-demo-continuity.cleanup.runtimes
picorv32-published-demo-continuity.cleanup.processes
picorv32-published-demo-continuity.cleanup.license-profiles
picorv32-published-demo-continuity.cleanup.mounts
picorv32-published-demo-continuity.cleanup.preserve-borrowed
picorv32-published-demo-continuity.cleanup.clean-pins
picorv32-published-demo-continuity.cleanup.remotes
picorv32-published-demo-continuity.evidence.finalize
picorv32-published-demo-continuity.ticket2.elab-sim_axi_zbb
picorv32-published-demo-continuity.ticket2.sim-sim_axi_zbb
```

### claude-supported-client

```text
picorv32-published-demo-continuity.interactive.supported-client
```
