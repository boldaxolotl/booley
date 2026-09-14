# Session Runtime issuance separation — #487

## Outcome

Session Runtime now owns spec sealing, issuance persistence, authentication,
validation, immutable image retention, and durable invalidation recovery.
`booley.eda` retains registration, exact Project grants, installation and license
resolution, relay topology, and Vivado compatibility policy. The narrow
`eda.provisioning.session_requirements` provider returns immutable values to
Runtime under one authority lease.

Flow enablement parsing moved to `booley.config.flow_enablement`. The former EDA
to Flow-execution edge is absent, including deferred and type-only imports, and
the D19 architecture rule prevents its return. D20 prevents EDA from acquiring
knowledge of Runtime issuance or invalidation.

## Before and after measurements

Both reports were generated with:

```console
python3 tests/architecture/report.py --source-root src/booley --top 30
```

| Measurement | Before (`b26efcc8`) | After (`acfa2af0`) |
| --- | ---: | ---: |
| Parsed Python modules | 483 | 487 |
| Located dependency facts | 2,371 | 2,392 |
| Unique normalized edges | 1,946 | 1,963 |
| Multi-package SCC members | 18 | 18 |
| Mutual package pairs | 15 | 15 |

The mutual-pair count is unchanged, but its composition improves: the
`EDA <-> Flows` pair disappears. `Config <-> EDA` appears because declarative
enablement is now below both EDA and Flows. `EDA <-> Runtime` remains for the
explicit requirements direction described below and for pre-existing shared
host-path/storage primitives; issuance no longer contributes the reverse edge.

## EDA / Runtime edge inventory

Before, EDA depended on Runtime through:

- `eda.config -> runtime.project_dir`;
- `eda.provisioning.authority -> runtime.auth_token, runtime.private_store`;
- licensing and Vivado policy modules `-> runtime.paths`;
- `eda.provisioning.runtime_spec -> runtime.auth_token, runtime.devcontainer,
  runtime.interactive_docker, runtime.platform_paths, runtime.timefmt`.

Runtime depended on EDA through `session_refresh`, `session_runtime`, and
`session_spec -> eda.provisioning.runtime_spec`, plus existing authority and
FlexNet topology calls from `session_runtime`.

After, no EDA module imports `runtime.session_issuance` or
`runtime.issuance_invalidation`, and no EDA module imports `booley.flows`.
The remaining EDA-to-Runtime facts are:

- `eda.provisioning.authority -> runtime.auth_token, runtime.private_store` for
  the established private host-state primitives;
- FlexNet licensing and Vivado policy `-> runtime.paths` for established packaged
  Runtime asset locations.

Runtime now calls `eda.provisioning.session_requirements` for resolved EDA facts.
It continues to call authority and FlexNet lifecycle APIs where Runtime must
validate or remove its own Docker resources. These are Runtime-to-EDA consumption
edges, not EDA knowledge of Runtime issuance.

## Composition hotspots

| Named hotspot | Before | After | Explanation |
| --- | ---: | ---: | --- |
| `booley.harness.doctor` | 66 | 66 | Unchanged. |
| `booley.harness.booley` | 58 | 59 | Adds the Grant adapter that supplies EDA callbacks to Runtime's coordinator. |
| `booley.harness.init_cmd` | 42 | 39 | Runtime now owns EDA input resolution and spec persistence behind the issuance interface. |
| `booley.harness.developer` | 45 | 45 | Unchanged. |
| `booley.flows.sim.flow` | 48 | 48 | Unchanged. |
| `booley.flows.synth.flow` | 35 | 35 | Unchanged. |
| `booley.mcp.server` | 31 | 31 | Unchanged. |
| `booley.flows.fpga.flow` | 32 | 32 | Unchanged. |
| `booley.specialists.mutation_tester` | 25 | 25 | Unchanged. |
| `booley.specialists.coverage_analyst` | 11 | 11 | Unchanged. |

## Preserved safety properties

- Exact canonical Project identity still keys stamps, grants, keeper images,
  Docker labels, relay resources, and cleanup.
- Existing version-4 stamps remain readable from the legacy EDA path; the next
  issuance migrates persistence to the Runtime-owned path.
- Spec/file digests, validator digest, immutable image ID and keeper verification,
  exact trusted mounts, network topology, license environment, policy revision,
  wrapper digest, and relay image identity remain validated.
- An inactive FPGA Flow with no prior EDA marker does not open the EDA authority
  store. Licensed, unlicensed, disabled, tampered, stale, and refresh-recovery
  paths remain covered.
- Grant mutation writes a Runtime invalidation journal before changing authority.
  Revocation removes authority first, then invalidates the stamp and removes
  exact Project-labeled containers before networks. Residual cleanup leaves the
  journal pending so the next locked host lifecycle operation retries it.

## Deep-module deletion proof

`booley.runtime.session_issuance` is the shared authority boundary for four
independent callers. Deleting it would force Project Initialization to recreate
builder input resolution and atomic issuance; Doctor to recreate stamp,
licence, label, and keeper validation; Session lifecycle to recreate
authentication and current-authority validation; and refresh recovery to
recreate snapshot decoding and predecessor/replacement authentication. The
checked architecture test names those callers and their distinct capabilities,
and also proves the EDA requirements provider does not reimplement Runtime's
issue, authenticate, or invalidate operations.

## Verification

The required final gate consists of `ruff check src/ tests/`, the complete
architecture suite and report, focused issuance/provisioning/recovery suites,
and the full non-Docker pytest suite. The PR records the final command results.

## Follow-up #531 — complete Config/EDA/Runtime source directions

Measured on 14 SEP 2026 against before source/analyzer `90c27b43` and after
source/analyzer `8efb7c56`. #530 is still open and its changes are not included.
These results replace the remaining-edge status above without rewriting the
historical #487 observations.

### Ownership and compatibility

- `config.eda` owns `EdaConfig`, `EdaConfigError`, the request schema/parser,
  and declarative Project loading. `config.settings` and `config.project_config`
  import those values directly and get retired checks from their existing owner,
  `config.flow_enablement`.
- `eda.provisioning.configuration` composes declarative loading with the existing
  Windows host-provisioning validation. Authority owns opaque installation/license
  name validation. Registration, grants, licenses, Vivado compatibility, authority
  leasing, and authority lifecycle remain in EDA.
- `core.private_store` and `core.file_lock` own the whole shared persistence
  mechanism. All five store callers (EDA authority, Project Inventory, Runtime
  issuance, invalidation, and refresh) use it and `core.user_paths.config_dir`.
  Each caller retains its original root, anchor, schema, errors, and ordering.
- `core.resources.package_data_dir` owns package lookup and the shadow-package
  fallback. Runtime still owns executable discovery and all issuance behavior.
- `eda.config`, `eda.__init__`, `runtime.private_store`, `runtime.file_lock`, and
  `runtime.paths.package_data_dir` preserve their supported imports through
  downward exports. Class, function, and exception identities are tested. Private
  monkeypatch targets moved to the implementation owners. No new import waiver
  or composition exception was introduced; D18–D20 remain intact.

Existing per-reader behavior remains explicit: settings logs unreadable/malformed
TOML and returns defaults; Project config stays lazy and memoized; EDA loading
wraps document errors and its composed loader applies platform policy. Retired
keys retain diagnostic precedence. Flow enablement still disables only on literal
`false`. Unhashable provisioning values retain their existing `TypeError`
rejection; diagnostic hardening is outside this refactor.

### Removed and replacement source edges

| Former source → target | Final dependencies replacing that knowledge |
| --- | --- |
| `config.settings → eda.config` | `config.eda`, `config.flow_enablement` |
| `config.project_config → eda.config` | `config.eda`, `config.flow_enablement` |
| `eda.provisioning.authority → runtime.auth_token` | `core.user_paths` |
| `eda.provisioning.authority → runtime.private_store` | `core.private_store` |
| `eda.provisioning.licensing.flexnet_docker → runtime.paths` | `core.resources` |
| `eda.provisioning.policies.vivado → runtime.paths` | `core.resources` |

There are now zero Config→EDA and zero EDA→Runtime source facts, including
function-local, conditional, relative, and type-only imports. D23 and D24 enforce
those complete prefix directions. D25 protects the three neutral mechanism
owners from higher-level policy imports. Runtime→EDA requirements consumption,
including the authority lease, remains intentional.

### Measured graph and caller fan-out

| Measurement | Before `90c27b43` | After `8efb7c56` |
| --- | ---: | ---: |
| Parsed Python modules | 492 | 497 |
| Located dependency facts | 2,424 | 2,438 |
| Unique normalized edges | 1,989 | 2,002 |
| Config→EDA facts | 2 | 0 |
| EDA→Runtime facts | 4 | 0 |
| Mutual package pairs | 13 | 11 |
| Largest cyclic package group | 18 | 18 |

Config↔EDA and EDA↔Runtime disappear from the direct mutual pairs. The remaining
pairs are B-Wave↔Flows, Dev Support↔Runtime, Feedback↔Harness, Flows↔Targets,
FuseSoC↔Runtime, FuseSoC↔Targets, Harness↔MCP, Harness↔Runtime,
Harness↔Ticket Board, MCP↔Specialists, and MCP↔Ticket Board.

The exact nontrivial SCC remains the approved 18-member group:

```text
agent_workspace, audit, bwave, config, criteria, dev_support, eda, feedback,
flows, fusesoc, harness, mcp, projects, review, runtime, specialists, targets,
ticket_board
```

Each name is under `booley`. In particular, Config still reaches Targets and
its unresolved Flow/Runtime paths, so removing the direct pairs alone cannot
split this group. No smaller SCC membership is claimed or approved. The new
production metadata test requires equality between approved and actual groups;
a future split, including #530, must tighten the metadata in that same change.
The SCC-only seeded tests independently reject joining the remaining group with
Core, Docker, Evidence, or Presentation. The existing analyzer tests also prove
that separate approved groups cannot recombine. The issue's 12-member/9-pair
projection remains cumulative with #530, not achieved by this branch.

| Module (under `booley`) | Before `90c27b43` | After `8efb7c56` |
| --- | ---: | ---: |
| `config.settings` | 7 | 8 |
| `config.project_config` | 5 | 6 |
| `config.eda` | 0 | 3 |
| `eda` | 1 | 2 |
| `eda.config` | 3 | 4 |
| `eda.provisioning.configuration` | 0 | 1 |
| `eda.provisioning.authority` | 4 | 3 |
| `eda.provisioning.session_requirements` | 4 | 5 |
| `eda.provisioning.licensing.flexnet_docker` | 2 | 2 |
| `eda.provisioning.policies.vivado` | 1 | 1 |
| `audit.project_schema` | 4 | 6 |
| `harness.doctor` | 66 | 66 |
| `projects.inventory` | 4 | 4 |
| `runtime.private_store` | 1 | 1 |
| `core.private_store` | 0 | 1 |
| `runtime.file_lock` | 0 | 1 |
| `core.file_lock` | 0 | 0 |
| `runtime.paths` | 0 | 1 |
| `core.resources` | 0 | 0 |
| `runtime.session_issuance` | 12 | 12 |
| `runtime.issuance_invalidation` | 6 | 6 |
| `runtime.session_refresh` | 10 | 10 |

Zero denotes no in-tree outgoing imports, including owners absent before this
change. Settings, Project config, requirements, and Audit now explicitly import
the distinct parser, retired-check, and host-policy owners instead of relying on
EDA's old mixed module. Authority's fan-out decreases because name validation is
local. Compatibility exports add edges without reintroducing prohibited
knowledge. Unchanged issuance/refresh fan-out reflects stable orchestration;
new Core locking/resources have no in-tree dependencies. These counts are
diagnostics, not limits.

### Reproduction

Run this for each revision; each archive contains its own source and analyzer:

```bash
for snapshot_ref in 90c27b43 8efb7c56; do
  snapshot_dir="$(mktemp -d)"
  git archive "$snapshot_ref" src/booley tests/architecture | tar -x -C "$snapshot_dir"
  python3 "$snapshot_dir/tests/architecture/report.py" \
    --source-root "$snapshot_dir/src/booley" --top 1000
done
```

The complete fan-out output includes every importing module; an absent owner has
zero outgoing imports. The raw six-edge inventory can be reproduced with
`analyze_imports` from each snapshot, filtering the source/target prefixes
`booley.config`/`booley.eda` and `booley.eda`/`booley.runtime`. The analyzer itself
has not changed; only executable contract metadata and its proofs were added.

### Validation

- Pre-move characterization and architecture/Config/Flow baseline: 179 passed.
- Focused architecture, Config, EDA, secure storage, locks, paths, Project
  Inventory, issuance, invalidation, Session lifecycle, refresh/recovery, image
  lifecycle, Init, and Doctor suites: 1,405 passed, 6 skipped.
- Secure-store tests prove POSIX owner/mode checks, anchor/intermediate/root/file/
  lock symlink rejection (lock no-follow flags are platform-dependent), nonregular
  input rejection, real contention and release
  after exceptions, atomic replacement, file-before-replace/directory-after-replace
  fsync ordering, and failure propagation with temporary-file cleanup.
- Existing issuance/provisioning/recovery tests retain exact Project identities,
  independent grants/licenses, inactive-Flow authority avoidance, legacy version-4
  stamps, image keepers, invalidation, and interrupted recovery behavior.
- Ruff 0.16.6 lint and formatting checks pass. Windows locking semantics retain
  their existing test file and its explicit Windows CI selection. Native and
  licensed/Docker qualification remain distinct from the Python tests.

- Full non-native Python suite: **11,293 passed, 51 skipped** in 106.11s.
  Command: `python -m pytest tests/ -q --tb=short -n 4 --dist=loadscope -m 'not native_bwave'`.
  The validation environment must be on PATH for subprocess `python` calls;
  its source path must select this worktree. The run used Python 3.13.14 and
  pytest 9.1.1, with local sockets permitted for B-Wave tests. Native B-Wave
  and optional installed/licensed EDA prerequisites were not qualified by this run.
- Final focused Core/architecture check after portable test adjustments:
  **154 passed**. `ruff check src/ tests/`, `ruff check .`, and
  `ruff format --check .` pass with Ruff 0.16.6.

### Merge preparation integration: 14 SEP 2026

PR #534's initial confidential-content CI rejected the author identities because
its base carried an empty trusted author allowlist. The same candidate identity
fails against the sealed policy at `90c27b43` and passes against current main's
policy. Main already contains the dedicated repair `69c326ab`; merging main at
`35062dda` into this branch produced `8f7d1479` without conflicts. No scanner
exception, identity change, or policy change was added by #531.

The final integrated source and analyzer have these reproducible measurements:

| Diagnostic | Integration base `35062dda` | Integrated source `8f7d1479` |
| --- | ---: | ---: |
| Python modules | 494 | 499 |
| Located dependency facts | 2,458 | 2,472 |
| Unique normalized edges | 2,017 | 2,030 |
| Config→EDA facts | 2 | 0 |
| EDA→Runtime facts | 4 | 0 |
| Mutual package pairs | 13 | 11 |
| Largest cyclic package group | 18 | 18 |

Use the same archive/report commands above with these two revisions. The
additional two modules and 34 dependency facts relative to the original snapshot
come from intervening main work, not this refactor. The removed directions,
remaining exact SCC, and affected owner fan-out are unchanged by integration.
