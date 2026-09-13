---
name: booley-add-to-qa
description: Classify proposed public Booley behavior and prepare reviewed Capability, Check, or Scenario changes.
---

# Add behavior to Booley public QA

Use this skill only when the user invokes it explicitly. Treat the prose following the
invocation as a candidate requirement, not as authority for the expected behavior.

## Read the contract

Read [`coverage.yaml`](../coverage.yaml), existing Scenarios that might cover the
behavior, and [`scenario.schema.json`](../scenario.schema.json), which is the structural
authority.
Read the shared [protocol](../doc/PROTOCOL.md) only when the proposed change depends
on or changes shared execution behavior, including when designing a new Scenario.

For a Runtime Attachment or Waveform Viewer requirement, also read the
[GUI Check example](references/gui-check-example.md).

## Classify the requirement

Search Capabilities, Checks, and Scenarios by meaning, not only by matching words.
Produce exactly one classification for the proposed change:

- `already-covered`: existing Checks fully exercise the behavior; make no changes and
  identify them.
- `correct-existing-check`: an existing Check is intended to exercise the behavior but
  its stimulus, expectation, evidence capture, evaluator, or Configured Scenario
  selection lets a relevant failure escape. Correct it against the public authority
  and identify the missed failure. Keep its ID when it still expresses the same claim.
- `existing-capability`: the behavior belongs to an existing Capability but needs one
  or more new Checks.
- `new-capability`: the behavior is a distinct supported product Capability and needs
  both a `coverage.yaml` entry and one or more Checks.
- `new-scenario`: the behavior spans multiple Capabilities and needs a distinct
  end-to-end workflow whose inputs, Steps, evidence, or recovery cannot fit coherently
  in an existing Scenario. Design the new Scenario and its Checks, adding Capability
  entries only for genuinely new supported behaviors. Size alone does not establish
  the need for a new Scenario.
- `not-ready`: the behavior lacks an authoritative public expectation, observable
  evidence, enough information to design a suitable Scenario, or another decision
  needed to write a trustworthy Check. State what is missing without inventing it.

Choose `not-ready` when the authority or evidence needed to design a trustworthy change
is missing. Otherwise choose `correct-existing-check` when an intended Check has a false
negative, even if the correction also requires a separate new Check. Choose
`new-scenario` when a distinct workflow is needed, even if it also introduces new
Capabilities. State those additional changes in the proposal.

Keep independently observable expectations in separate Checks. Do not weaken, merge,
remove, or exclude existing coverage to accommodate the request. Preserve stable IDs;
assign a new semantic uppercase-kebab ID only for a genuinely new Capability.

## Preserve the suite contract

- Use `format_version: 1`. Keep Scenario, Step, and Check IDs stable, qualify external
  Check references with the Scenario ID, and never repurpose an ID.
- Let `coverage.yaml` own Capabilities and their public sources. Let Scenario files own
  Checks, Check sets, and Configured Scenario assignments. Derive the reverse coverage
  index from the Check references instead of recording duplicate assignments or a
  parallel coverage model.
- Keep Check sets flat and disjoint. Assign every Check to exactly one set and select
  every set from at least one Configured Scenario.
- Keep `git` and `sha256` input identities as full lowercase hashes. A `pre-run` input
  fixes an identity; it cannot change a pinned IP, workload, or threshold.
- Keep asset paths inside their declared Scenario or shared base. Give every asset an
  audience and every production asset a SHA-256 digest. Template substitutions cannot
  change thresholds or disclose private assets.
- Keep restoration independent of the detection Check's success and keep product
  cleanup independently reachable. Reserve enough time for final records and safe
  shutdown and cleanup of run-owned resources.
- Keep Configured Scenario parameters, pre-run requirements, Check sets, and justified
  exclusions explicit. Include prerequisite Checks and supporting Steps in the same
  Scenario Run; unlisted setup and artifacts from earlier runs cannot replace them.

## Design the QA change

For any classification requiring an edit, select or design the Scenario, phase, Step,
named Check set, and Configured Scenarios that can produce the required evidence. For
`correct-existing-check`, explain why the current Check misses the failure and how the
corrected Check and evaluator distinguish pass from fail. For `new-scenario`, explain
why existing Scenarios cannot coherently host the workflow and design its inputs,
phases, Steps, Checks, Check sets, and Configured Scenarios. Account for:

- public expectation authority versus navigation-only references;
- stimulus, expected result, evidence, and capture location;
- prerequisite and supporting work;
- fault detection, restoration, and independently reachable product cleanup;
- literal payloads, permitted delegation, referenced assets, and asset hashes;
- run-owned resources, the complete 480-minute allocation, and final resource-cleanup
  effects; and
- every applicable Configured Scenario and any justified exclusion.

Do not treat Capability mapping, environment availability, or a prior run as evidence
that a new or corrected Check passes.

Apply these authoring rules:

- A specification-backed Check becomes required when the Human Maintainer approves
  it; it need not pass a reference run first.
- Every supported Capability needs a Check. Keep unresolved gaps visible.
- Preserve the complete fault, detection, restoration, and cleanup sequence for a
  seeded fault.
- Link known defects to their issues and preserve their failed evidence.
- Keep a landed fix pending verification until its applicable Checks pass.
- Classify flaky failures by cause and justify targeted validation; there is no fixed
  three-clean-runs rule.

Do not create separate candidate-assertion or Known Condition expiry workflows.

## Approval gate

Before editing, show the Human Maintainer one complete proposal containing:

- the classification and its rationale;
- the authoritative contract source;
- every Capability addition or reuse;
- every proposed or corrected Check with its complete evidence contract, including the
  failure an existing Check missed;
- placement in existing or new Scenarios, Steps, Check sets, and Configured Scenarios;
- for a new Scenario, why existing Scenarios are unsuitable and its complete workflow;
- affected assets, budgets, recovery, and cleanup behavior; and
- affected files and required Scenario reruns.

Ask: **Apply this QA change? (yes / edit / cancel)**

Do not modify QA assets until the Human Maintainer explicitly approves this proposal.
If the answer is `edit`, revise and present the complete proposal again.

## Apply and validate

After approval, make only the approved changes. Add a `coverage.yaml` entry for each
genuinely new Capability, including one introduced by a `new-scenario`. Preserve a
corrected Check's ID when its product claim is unchanged; use a new ID for a distinct
claim. Update production asset hashes only after their content is final.

Run the full structural checks with the repository's available Python interpreter:

```sh
python qa/validate.py
python -m pytest tests/qa/
```

Also generate and inspect a whole-suite coverage index with
`python qa/validate.py --coverage-index <temporary-path>`. The scenario filter cannot
produce that whole-suite index.

Classify the finished edit as behavioral or editorial. New Capabilities, new or corrected
Checks, and new Scenarios are behavioral. Report the required affected-Scenario reruns,
but do not execute Scenario Runs unless the user separately invokes `booley-qa-run`.
Do not push, open a pull request, or publish reports unless the user separately requests
that action.
