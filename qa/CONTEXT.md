# Public QA glossary

This is the canonical vocabulary for Booley's public QA missions. Shared
product concepts are defined in the [shared glossary](../docs/CONTEXT.md), and
**Finding** is defined in the [Feedback glossary](../src/booley/feedback/CONTEXT.md).

## Language

**Mission**:
One timeboxed, repeatable bug hunt that exercises Booley against pinned inputs.
It orders Mission Areas by priority but does not certify the product or require
every area to complete.
_Avoid_: Scenario, qualification suite, checklist

**Mission Area**:
One budgeted focus within a Mission, stating an intent, useful stimuli and
faults, known traps, and any real dependencies. Mission Areas are independent
unless the Mission says otherwise.
_Avoid_: Check, Criterion, protocol stage

**QA Run**:
One execution of a Mission against an exact Booley build on one host and agent
client. It preserves Findings, a running log, owned-resource cleanup state, and
local evidence under its run directory.
_Avoid_: Scenario Run, Qualification, certification

**Release Smoke List**:
The small must-pass list run before a release. Unlike a Mission, it has a binary
verdict: every item must pass as documented without a workaround.
_Avoid_: Qualification, Mission, regression suite

**Capability Map**:
The mapping from supported Booley behavior to the Mission Areas that exercise
it. It records QA breadth, not execution evidence and not native RTL coverage.
_Avoid_: Capability Coverage, Coverage Campaign, coverage result
