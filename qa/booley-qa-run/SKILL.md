---
name: booley-qa-run
description: Execute one evidence-producing Booley public QA Scenario Run.
---

# Run Booley public QA

Use this skill only when the user invokes it explicitly.

Follow the shared [protocol](../doc/PROTOCOL.md). It is the sole execution entry
point and discloses one Protocol Stage at a time. Finish by sealing Check Results,
Observations, evidence, and resource status. Do not create Findings or outcomes;
tell the Human Maintainer to invoke `booley-qa-triage` separately.

For an agent-driven attempt with declared milestones, retain the last milestone and
classify the terminal boundary as `terminal product failure`, `client/provider error`,
`declared timeout`, `operator stop`, or `lost control`. These are evidence categories,
not additional Check Result statuses. Follow Execute for whether independent work may
continue and Record for the resulting `fail` or `blocked` status.
