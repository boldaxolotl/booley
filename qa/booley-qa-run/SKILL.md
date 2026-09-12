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
