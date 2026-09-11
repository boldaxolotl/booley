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

| Measurement | Before (`b26efcc8`) | After |
| --- | ---: | ---: |
| Parsed Python modules | 483 | 487 |
| Located dependency facts | 2,371 | 2,389 |
| Unique normalized edges | 1,946 | 1,959 |
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

## Verification

The required final gate consists of `ruff check src/ tests/`, the complete
architecture suite and report, focused issuance/provisioning/recovery suites,
and the full non-Docker pytest suite. The PR records the final command results.
