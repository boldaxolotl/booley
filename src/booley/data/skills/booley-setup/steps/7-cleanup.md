# Step 7 — Cleanup: bounded retention after the report

> Part of the `booley-setup` skill. Run after Step 6 has rendered
> `SETUP-REPORT.md` and completed its one-time Feedback offer. This step is
> post-gate: incomplete or unsafe cleanup is reported plainly and never turns
> a healthy Doctor result into a failed setup.

Step 7 is the Project Setup retention pass. It does not garbage-collect a
Project. The product cleanup helper accepts only a manifest created by this
setup run, previews an immutable plan, and applies that plan only after
revalidating every candidate. The helper owns path safety, byte accounting,
same-filesystem quarantine, and its recovery journal; this skill never writes
`rm`, `git clean`, an ignored-file sweep, or a recursive deletion command.

## 1. Resume the ledger

Read the §3 **Execution ledger** in `SETUP-PLAN.md`.

- If it has a run ID and manifest path, reuse them. A cleanup retry never
  creates a second manifest and never adopts newly discovered paths.
- If the plan is `executing`, resume Step 7 at its recorded status and let the
  helper resume its recovery journal.
- If the plan is `executing` or `complete` but has no ledger, run an
  inventory-only preview. This legacy plan has no ownership authority; existing Projects are not retroactively granted
  ownership.
- A full setup allocates the run root before execution with:

  ```console
  booley cleanup prepare --run-id <ledger-run-id>
  ```

  The root is `.booley_project/tmp/setup/<run-id>/`. Every setup-authored
  detached stdout/stderr capture, exit-code sidecar, probe, conversion, and
  agent note is placed beneath it and registered before cleanup can remove it:

  ```console
  booley cleanup record <path> --run-id <run-id> --producer <step-command> \
    --class command-capture --disposition remove
  ```

  Flow output stays in its product-owned runtime/report location. Do not invent
  environment variables to move it. A Flow slot is reusable cache, not
  run-owned evidence.

## 2. Preview before mutation

Read rows 22 and 23 from the approved plan. Unattended setup uses
`minimal` + `preserve`; the plan's explicit deviation rule applies if either
value changes.

```console
booley cleanup preview --run-id <run-id> --retention minimal \
  --cache-disposition preserve
```

Print and record the returned digest in the ledger. Show counts and byte totals
grouped as:

- **Preserve:** configuration, authored cores/scripts, waivers, Tickets,
  reports, `SETUP-PLAN.md`, `SETUP-REPORT.md`, `PARITY-REPORT.md`, Findings
  semantics, Doctor stamps, active Job state, and structured evidence needed by
  a retained Finding or report.
- **Remove:** only manifest-owned current-run captures, exit-code sidecars,
  probes, bytecode, duplicate conversions, and terminal Doctor scratch under a
  bounded leaf subtree. The shared `tmp/` root is never a candidate.
- **Evict-cache:** only when row 23 explicitly says
  `evict-setup-touched`; this is delegated to the Flow cache owner with its
  lease, generation, and retention rules and includes the rebuild consequence.
- **Unresolved:** pre-existing or unmanifested residue, active-use claims,
  raw evidence still referenced by structured Findings, changed identities,
  unsafe symlinks, corrupt logs, and permission or quarantine failures.

The `diagnostic` mode preserves raw current-run evidence while still removing
disposable probes, duplicate captures, and bytecode. It does not authorize
deletion of anything that was present before this run.

## 3. Materialize Feedback attachments

`findings.jsonl` attachments are live paths: report, preview, export, and
submit re-read them later. Before removing an attached raw log, the helper's
Feedback operation snapshots exactly the bounded rendered evidence, including
source digest, original path, line/byte counts, and clipping metadata, under
`.booley_project/setup-evidence/`. It then atomically retargets only the
structured attachment field.

The operation preserves every Finding ID, semantic field, ordering, filed
state, and unattached byte. A corrupt Findings Log blocks migration and the
referenced source's deletion, but does not block unrelated safe candidates.
The helper verifies equivalent local report and maintainer-preview attachment
renders before and after materialization. Free-text path-like prose in
`repro`, `observed`, notes, or reports is never treated as a dependency.

## 4. Apply and close out

Apply only the digest just previewed:

```console
booley cleanup apply --run-id <run-id> --digest <preview-digest> \
  --retention minimal --cache-disposition preserve
```

At mutation time the helper rechecks that every candidate is still beneath the
same `.booley_project`, has the recorded no-follow file type, device, and inode,
has no live Job/process claim, and can move to a uniquely named sibling
quarantine on its own filesystem. It deletes only after rechecking the
quarantined identity. Missing candidates are recorded as already absent;
changed or replaced candidates become unresolved. An interrupted operation is
safe to retry because the manifest journal reconciles pending candidates and
quarantine state.

Append a concise disposition to the ledger: bytes removed, cache bytes evicted,
retained classes, raw evidence intentionally retained, rebuild consequences,
and unresolved items. Mark Step 7 complete only when the helper has returned;
unresolved items are a visible closeout result, not a hidden failure.
