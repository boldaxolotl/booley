---
name: booley-qa-run
description: Run a Booley QA mission — a timeboxed bug hunt on a pinned IP against a candidate Booley build — and write findings for later triage.
---

# Run a Booley QA mission

You are the **operator**. Your job is to find as many real Booley bugs, doc
errors, and friction points as the mission's timebox allows. The mission tells
you *where* to look; you decide *how* to get there when the documented path
breaks.

Inputs from the user: a mission name (`picorv32`, `taxi`, `uart`, `coverage` — a directory
under `qa/missions/`), a Booley build (an exact commit or a published version),
an artifact root, and optionally `smoke` to also run the release smoke list.

## Operating principles

- **Hunt.** Every area is an invitation to find what breaks. Try the documented
  path first, then edges: wrong inputs, repeats, interruptions, recovery.
- **Work around, then continue.** When something fails, record a finding, then
  route around it: retry, adapt the input, take another documented route, create
  the missing state by hand, or patch the mission's input inside the run dir.
  Write the workaround into the finding. Skip an area only when no workaround
  exists, and say why in `log.md`.
- **Mission bugs are findings.** A wrong pin, a stale prompt, or a pre-baked
  value that no longer matches Booley's output is a `qa-bug` finding. Fix it
  locally and keep going.
- **Timebox.** Each area has a budget. When you have spent it, log where you got
  to and move to the next area. Areas are independent unless the mission says
  otherwise.
- **Docs first.** Drive Booley through the documentation and packaged skills of
  the build under test, `--help`, and ordinary Project inspection. Read Booley
  source only to classify a finding you have already recorded.
- **Sub-agents** may take independent areas; give each the hard rules, the area
  text, and the run dir, and merge their findings into `findings.md`.

## Hard rules

- Keep all changes local: no `git push`, issue or PR creation, or other external
  submission.
- Leave Booley source unchanged; workarounds live in the run dir.
- Keep credentials and tokens out of evidence, prompts, and Projects. Redact
  secrets from captured output before saving it.
- Preserve borrowed state: credentials, shared images and caches, EDA installs,
  and worktrees or containers you did not create. The canonical host Booley
  install may change only through step 2's Human Maintainer-approved
  `origin/main` path; that replacement stays installed after the run and is not
  a `resources.md` row.
- Baseline IP and Project pins stay fixed; change designs only through the
  mission's prompts and Tickets.
- Before creating anything outside the run dir (container, image, worktree,
  branch, registration, grant, remote, mount, background process), append it to
  `resources.md`. Cleanup releases exactly those rows.

## Run directory

Create `<artifact-root>/<mission>-<client>-<UTC yyyymmddThhmmssZ>-<booley-commit[:8]>/`:

| File | Contents |
|---|---|
| `log.md` | Identity header, then a running log: area status, time spent, workarounds, cleanup result, smoke table |
| `findings.md` | One entry per finding (format below) |
| `resources.md` | Append-only table: resource, identity, how to release, released (yes/no/failed) |
| `evidence/` | Raw command output and artifacts, secrets redacted, one subdir per area |

The `log.md` identity header: Booley version and commit, wheel SHA-256, `qa/`
commit, client name and version, host OS, mission, timebox, start time.

Finding entry:

```markdown
## F-<n>: <one-line title>
- Area: <area slug>   Kind: bug | doc | friction | qa-bug | wish   Severity: high | medium | low
- Repro: <exact commands / steps>
- Expected: <what docs or common sense say>   Actual: <what happened>
- Output: <short excerpt that demonstrates the actual behavior>
- Workaround: <what you did to continue, or "none">
- Evidence: `evidence/<area>/<file>`
```

Write each finding the moment you observe it, before any workaround or retry.

## Procedure

1. **Disk.** Follow [DISK.md](../DISK.md). Low space after it is a warning in
   `log.md`, and the run continues.
2. **Confirm the canonical host install.** Run `git fetch origin main`, resolve
   `git rev-parse origin/main`, and compare that commit with the source commit
   reported by the canonical host `booley --version`. If they match, record the
   identity header and continue. If they differ, ask the Human Maintainer to
   choose one of these paths:
   - test the installed build and record its identity in the header; or
   - install `origin/main` as the canonical host install and run
     `booley bootstrap`.
   Record the choice in `log.md` before continuing.
3. **Smoke** (when requested). Walk [SMOKE.md](../SMOKE.md) and put its table in
   `log.md`.
4. **Mission.** Read `qa/missions/<mission>/MISSION.md` and work its areas in
   order under the principles above. Keep `log.md` current after each area:
   `done`, `partial`, or `skipped`, with the reason.
5. **Cleanup.** Run the mission's cleanup area (Booley's own cleanup is under
   test), then release every remaining `resources.md` row and mark it. Keep the
   run dir. A release you cannot complete is marked `failed` with a note, and
   is reported to the user.
6. **Report.** Make sure `findings.md` and `log.md` are complete and tell the
   user: finding count by kind and severity, areas skipped, cleanup failures,
   smoke verdict, and that `booley-qa-triage` turns the findings into issues.

The run is complete when every area is `done`, `partial`, or `skipped` with a
reason, every `resources.md` row is released or marked `failed`, and the report
is delivered.

## Resuming

After context compaction or a hand-over, reread this file, the mission,
`log.md`, and `resources.md`, then continue from the first area that is not
`done`. An operator that cannot continue the mission runs step 5 and step 6.
