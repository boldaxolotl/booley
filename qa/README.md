# Public Booley QA suite

The `qa/` directory defines Booley's public qualification suite. It contains
three production Scenarios with their Configured Scenarios, the capability
coverage map, format validation, and the rules for running and reporting QA.

Canonical suite terminology is defined in the [Public QA glossary](CONTEXT.md).
The repository-wide [context map](../CONTEXT-MAP.md) separates it from shared
Booley, Ticket Board, and B-Wave vocabulary.

## Why this exists

Booley has one maintainer and no human QA team. Release QA therefore has to be work
that agents can execute repeatably and a maintainer can audit afterward. This suite
defines the scope, instructions, evidence, and verdict rules needed to do that without
letting an agent decide for itself what counts as a pass.

Agents perform the setup, product exercises, fault injection, recovery, evidence
collection, and cleanup. The Human Maintainer still owns the Scenarios, required
Configured Scenarios, acceptance rules, and final qualification decision.

## Starting a run

Explicitly invoke the user-only [`booley-qa-run` skill](booley-qa-run/SKILL.md) with a
Scenario ID, a Configured Scenario ID, and an artifact root. The skill creates a fresh
Scenario Run ID. Invocation grants authority for the resources and mutations
declared by the selected Scenario; anything outside that scope requires separate
user authorization. The agent never invokes this skill on its own.

QA execution is an agent skill, not a Booley CLI command. The agent executing the
skill is the Scenario Operator: it follows the selected Scenario and protocol,
delegates work to sub-agents where useful, and owns the evidence record. `validate.py`
checks the authored QA assets only; it does not execute a Scenario Run. Once the
required inputs are available, the skill runs unattended until completion, a declared
stop condition, or a request for authority outside the Scenario's declared scope.

## What's in this directory

| Path | Contents |
|---|---|
| [`scenarios/`](scenarios/) | Production scenario YAML plus each Scenario's prompts, Ticket payloads, probes, fixtures, specifications, and evaluator material |
| [`shared/probes/`](shared/probes/) | Probe contracts used by more than one scenario |
| [`coverage.yaml`](coverage.yaml) | Product capability inventory and public contract sources |
| [`scenario.schema.json`](scenario.schema.json) | Structural contract for scenario files |
| [`validate.py`](validate.py) | Offline validation of structure, references, Configured Scenarios, asset hashes, prerequisites, fault recovery, budgets, and coverage |
| [`booley-qa-run/`](booley-qa-run/) | Skill that coordinates an evidence-producing Scenario Run |
| [`agents/`](agents/) | Shared execution protocol and run-record format used by the skill |
| [`user/`](user/) | Maintainer guides for qualification and scenario authoring |
| [`examples/`](examples/) | Illustrative Configured Scenario, Scenario Run record, results, and summary; they are not execution evidence and grant no coverage credit |

The current suite maps 62 product capabilities and 16 distinct EDA integration
references to 1,072 checks. Those counts show that the reviewed requirements are
represented. They do not prove that Booley passes them.

## Scenarios

| Scenario | What it exercises | Sources |
|---|---|---|
| PicoRV32 published demo continuity and evolution | Starts from the pinned published-demo Project and upstream source. It checks clean simulation, lint, synthesis, provisioned Linux Vivado, Interactive Mode and waveform diagnosis, two Ticket Mode changes, final regression, and cleanup. | [Scenario](scenarios/picorv32/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/374) |
| Taxi 10G MAC port and evolution | Starts from a pinned direct clone of Taxi. It checks Project setup, the clean 10G MAC baseline, FST and B-Wave behavior, a disposable submodule companion Project, mutation testing, two Ticket Mode changes, final regression, and cleanup. | [Scenario](scenarios/taxi/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/377) |
| Documentation-only standalone UART | Builds a UART from the allowlisted OpenTitan documentation corpus without giving the developer the reference implementation or oracle. It covers Interactive Mode, feature and repair Tickets, independent evaluator controls and cases, external-image handling, final regression, and cleanup. | [Scenario](scenarios/uart/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/375), [oracle contract](scenarios/uart/evaluator/CONTRACT.md) |

The scenarios retain their reviewed pins, workloads, thresholds, prompts, authority,
fault and recovery sequences, independent evaluation, and cleanup rules. The
[published design](https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/HANDOFF.md)
records the historical decisions behind the production files.

## QA workflow

1. Select a Configured Scenario declared by its `scenario.yaml`. It fixes the host operating
   system, CPU architecture, native-host scope, agent provider, Interactive Mode
   client, Ticket Mode backend, pre-run requirements, check sets, and exclusions
   before execution. The validator resolves the sets into the complete Check list and
   derives the required Scenario Steps.
2. Validate the suite structure. Validation catches structural and reference errors;
   it does not execute a scenario or judge whether an expectation is a sound product
   oracle.
3. Prepare the run. Record the exact Booley product revision and artifact, matching docs and image,
   suite and input revisions, native host, provider, EDA provisioning, authority,
   deadline, artifact root, and capability probes in `run.json`. The artifact form
   must match the Configured Scenario; undeclared substitutions are invalid.
4. Execute the selected checks in scenario order. Stay within the declared authority
   and retry limits. Capture the expected observation and the required artifact for
   each check. Preserve unexpected failures even when recovery or a later retry
   succeeds.
5. Record Check Results as defined in [`agents/FORMAT.md`](agents/FORMAT.md). Keep `results.jsonl` and
   `findings.jsonl` append-only, track resources in `cleanup-ledger.json`, retain
   immutable artifacts under `evidence/`, and derive `summary.md` from those records.
6. Clean up on every exit path, then calculate the Scenario Run Outcome. A
   trustworthy required failure makes it `failed`. Missing, blocked,
   unavailable, or invalid required evidence makes it `incomplete`. It is `passed`
   only when all selected Checks and cleanup pass.

GUI Configured Scenarios require the supported VS Code client, WCP, and a qualified screenshot
observer. If that infrastructure is missing, the affected checks are unavailable and
the Scenario Run Outcome is incomplete. A headless substitute does not earn GUI credit, and
the Scenario Run cannot be narrowed after it starts.

## Validate changes

Install the validator dependencies, then run the whole-suite check:

```sh
python -m pip install -r qa/requirements-validation.txt
python qa/validate.py
```

During authoring, validate one scenario or write the derived whole-suite coverage
index for review:

```sh
python qa/validate.py --scenario picorv32-published-demo-continuity
python qa/validate.py --coverage-index /tmp/booley-qa-coverage.json
```

The scenario filter cannot produce a whole-suite coverage index. Regression tests
retain a contract for reviewed Configured Scenario parameters, requirements,
membership, and exclusions. After
changing QA assets, also run `python -m pytest tests/qa/` as required by the
[authoring guide](user/AUTHORING.md).

## Current status

The repository contains the production Scenarios and validation tooling, but no
full qualification results. The exact, immutable Booley build under test may be a
published release or an unreleased candidate, with package/image provenance and a
matching documentation snapshot. Execution also requires the reference native hosts,
provider access, authorized disposable resources, declared EDA provisioning, and
independent evidence storage. GUI Configured Scenarios require actual supported VS Code
clients, WCP, and a qualified screenshot observer; no headless substitute or narrower
configuration is implied. Record implementation progress in the relevant pull request
or issue, and put execution results in run records.

For the UART scenario, exact-a/VAL=32 timeout comparisons use the approved public
30 to 34 bit-time window in the [timing addendum](scenarios/uart/spec/timing-addendum.md).
Other timing observations remain blocked when the public contract cannot establish a
verdict.
