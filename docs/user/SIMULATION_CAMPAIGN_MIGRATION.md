# Simulation Campaign migration

Simulation execution now creates a durable, immutable Simulation Campaign for
each Target. This is a deliberate public-interface break for exact selection
and a storage change for scripts that consume Simulation reports.

## Invocation changes

| Before | Now |
|---|---|
| one CLI `--test NAME` value | repeat `--test NAME` for an exact ordered suite |
| per-run CLI `--skip NAME` | no CLI option; use persistent `tests.toml` `skip`, or name the exact suite to run |
| MCP `test: "reset"` | MCP `test: ["reset"]` |
| rerun and infer the latest result | save the printed manifest path and use `--resume-from MANIFEST` |

`--tests-file PATH` is CLI-only. It reads one exact test name per UTF-8 line,
ignores blank lines and lines whose first non-space character is `#`, and
rejects an empty, duplicate, or unknown selection. It cannot be combined with
`--test`. MCP callers send the normalized names directly as a nonempty unique
array and must not send the old scalar shape.

`--resume-from` accepts only the exact immutable `campaign/manifest.json`. Do
not also send Target, test selection, explicit mode, coverage, or trace: resume
reconstructs those values from the manifest. Timeout, diagnostic, dry-run, and
presentation controls remain invocation-local.

## Report consumers

Save the pointers returned for each Target instead of constructing filenames:

- `manifest` is the immutable resume authority;
- `summary` is the ordered Campaign status and result projection;
- `simulation` is the compatibility projection for existing consumers;
- `coverage`, when present, is the canonical Target-level authenticated
  reference to the nested Coverage Campaign.

The bounded MCP response exposes observation counts and at most 32 observation
previews. Read the summary and bound result artifacts when complete evidence is
needed. Existing `simulation.json` compatibility keys remain, with independent
`execution`, `functional`, and `assertions` observations added for each test.

Scripts must not discover a “latest” Campaign, edit Campaign JSON, or copy an
attempt/result between Campaigns. Immutable version-1 Simulation Campaign files
are either read exactly or rejected with an unsupported-version diagnostic;
they are never rewritten during upgrade.

## Project configuration

The default `pre_sim_build_access = "immutable"` shares one authenticated
Simulator Bundle per compatible build variant and withholds its build path from
Pre-Sim Commands. Projects whose hook must modify compile inputs must opt into
`"legacy-per-test"`; those work items receive private builds and forgo sharing.

A literal `[flows.sim].run_cwd` must already exist and serializes colliding
work. Add `{campaign}`, `{target}`, `{test}`, or `{attempt}` to request a
Booley-owned isolated attempt directory. `[jobs].max_heavy` bounds total
Project-local simulator concurrency, counting the already-admitted outer
Simulation Job; its default of one remains serial.

## Recovery and retention

Preview recovery before mutating anything:

```bash
booley flow sim --resume-from /exact/campaign/manifest.json --dry-run
booley flow sim --resume-from /exact/campaign/manifest.json
```

Completed work does not rerun. Interrupted ordinary HDL work receives a new
attempt; an interrupted Cocotb batch or coverage aggregate reruns at that whole
disclosed granularity. Corrupt, linked, truncated, oversized, noncanonical, or
digest-mismatched authority fails with exit 2 before EDA launch. Restore exact
bytes or start a new Campaign; do not patch the evidence.

Retention protects incomplete or invalid Simulation Campaigns. A complete
Campaign can be archived or pruned only through exact known-file retention;
coverage retention follows the authenticated Target-level reference to its
nested attempt-scoped Coverage Campaign.
