# Step 7 — Cleanup: remove the run's scratch root after the report

> Part of the `booley-setup` skill. Run after Step 6 has rendered
> `SETUP-REPORT.md` and any explicitly requested Feedback export. This step is
> post-gate: a kept or refused cleanup is reported plainly and never turns a
> healthy Doctor result into a failed setup.

Step 7 removes one directory: the scratch root `.booley/tmp/setup/<run-id>/`
that this run created. The helper owns path safety; this skill never writes
`rm`, `git clean`, an ignored-file sweep, or a recursive deletion command.

## 1. Resume the ledger

Read the §3 **Execution ledger** in `SETUP-PLAN.md`. With no run ID there is
nothing to clean: report that and stop. Step 0 already created the root with
`booley cleanup prepare` and recorded its run ID; do not prepare it again (a
second `prepare` refuses because the root exists).

Every setup-authored capture, exit-code sidecar, probe, conversion, and
Developer Agent note goes beneath that root; nothing is registered. Flow output
stays where the Flow writes it. Evidence worth keeping (reports, golden files)
is written outside the root.

## 2. Preview and apply

```console
booley cleanup preview --run-id <run-id>
```

Preview lists every file under the root with its size, marks files a Finding
attaches, and changes nothing. Then follow row 21 of the approved plan:

- `minimal`: `booley cleanup apply --run-id <run-id>`. Attachments under the
  root are first snapshotted to `.booley/setup-evidence/` and retargeted;
  Finding IDs and rendered reports stay the same. Nothing outside the root is
  removed, including the Flow cache and Doctor scratch written elsewhere.
- `diagnostic` skips `booley cleanup apply`: report the retained run root.

Exit codes: 0 removed or already gone, 1 kept, 2 refused (nothing deleted:
unsafe or symlinked path, a run root without its ownership marker, or an
unreadable Findings Log). Exit 1
prints each reason and path: fix the named Finding, attachment, or permission
and rerun `apply`; retries are safe. Then record the bytes removed (or the
retained root and why) in the ledger's **Final disposition**.
