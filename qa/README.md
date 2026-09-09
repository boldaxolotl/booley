# Public Booley QA suite

The `qa/` directory defines Booley's public release qualification suite. It contains
three production scenarios, the profiles that select their checks, the capability
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
collection, and cleanup. The maintainer still owns the scenarios, qualification
profiles, acceptance rules, and final release decision.

## Starting a run

Point the agent to [`agents/RUN.md`](agents/RUN.md) and give it a profile ID, a run ID,
an artifact root, and the authority required by that run. The entry point includes a
prompt template and the completion criteria.

The suite is executable, but there is no single `booley qa run` command that
orchestrates it. `validate.py` checks the authored definitions only. During an actual
run, a coordinator agent follows the selected scenario and protocol, delegates work
where useful, and writes the evidence record. Once the operator has supplied the
inputs and authority, the run proceeds unattended until completion, a declared stop
condition, or a request for authority that was not granted at the start.

## What's in this directory

| Path | Contents |
|---|---|
| [`scenarios/`](scenarios/) | Production scenario YAML plus each journey's prompts, Ticket payloads, probes, fixtures, specifications, and evaluator material |
| [`shared/probes/`](shared/probes/) | Probe contracts used by more than one scenario |
| [`profiles.yaml`](profiles.yaml) | Required runs and named check sets for each platform, provider, and core or GUI scope |
| [`coverage.yaml`](coverage.yaml) | Product capability inventory and public contract sources |
| [`scenario.schema.json`](scenario.schema.json) | Structural contract for scenario files |
| [`validate.py`](validate.py) | Offline validation of structure, references, profile selections, asset hashes, prerequisites, fault recovery, budgets, and coverage |
| [`agents/`](agents/) | Agent entry point, execution protocol, and run-record format |
| [`user/`](user/) | Maintainer guides for qualification and scenario authoring |
| [`examples/`](examples/) | Illustrative scenario, profile, result, and summary files; they are not execution evidence and grant no coverage credit |

The current suite maps 62 product capabilities and 16 distinct EDA integration
references to 1,072 checks. Those counts show that the reviewed requirements are
represented. They do not prove that Booley passes them.

## Scenarios

| Journey | What it exercises | Sources |
|---|---|---|
| PicoRV32 published demo continuity and evolution | Starts from the pinned published-demo Project and upstream source. It checks clean simulation, lint, synthesis, provisioned Linux Vivado, Interactive Mode and waveform diagnosis, two Ticket Mode changes, final regression, and cleanup. | [Scenario](scenarios/picorv32/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/374) |
| Taxi 10G MAC port and evolution | Starts from a pinned direct clone of Taxi. It checks Project setup, the clean 10G MAC baseline, FST and B-Wave behavior, a disposable submodule companion Project, mutation testing, two Ticket Mode changes, final regression, and cleanup. | [Scenario](scenarios/taxi/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/377) |
| Documentation-only standalone UART | Builds a UART from the allowlisted OpenTitan documentation corpus without giving the developer the reference implementation or oracle. It covers Interactive Mode, feature and repair Tickets, independent evaluator controls and cases, external-image handling, final regression, and cleanup. | [Scenario](scenarios/uart/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/375), [oracle contract](scenarios/uart/evaluator/CONTRACT.md) |

The scenarios retain their reviewed pins, workloads, thresholds, prompts, authority,
fault and recovery sequences, independent evaluation, and cleanup rules. The
[published design](https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/HANDOFF.md)
records the historical decisions behind the production files.

## QA workflow

1. Select profiles from [`profiles.yaml`](profiles.yaml). A profile fixes the platform,
   provider, scenario runs, named check sets, exclusions, and capability requirements
   before execution. Core product behavior and GUI/client integration have separate
   verdicts. The validator resolves the sets into the complete check list and derives
   the required scenario steps.
2. Validate the suite definition. Validation catches structural and reference errors;
   it does not execute a scenario or judge whether an expectation is a sound product
   oracle.
3. Prepare the run. Record the exact released Booley package and image, matching docs,
   suite and input revisions, native host, provider, EDA provisioning, authority,
   deadline, artifact root, and capability probes in `run.json`. Local wheels,
   editable installs, development builds, and source-checkout imports are not valid
   release inputs.
4. Execute the selected checks in scenario order. Stay within the declared authority
   and retry limits. Capture the expected observation and the required artifact for
   each check. Preserve unexpected failures even when recovery or a later retry
   succeeds.
5. Record results as defined in [`agents/FORMAT.md`](agents/FORMAT.md). Keep `results.jsonl` and
   `findings.jsonl` append-only, track owned resources in `resources.json`, retain
   immutable artifacts under `evidence/`, and derive `summary.md` from those records.
6. Clean up on every exit path, then calculate each profile verdict. A trustworthy
   required failure makes the profile `failed`. Missing, blocked, unavailable, or
   invalid required evidence makes it `incomplete`. A profile is `passed` only when
   all required checks and cleanup pass.

GUI profiles require the supported VS Code client, WCP, and a qualified screenshot
observer. If that infrastructure is missing, the affected checks are unavailable and
the profile is incomplete. A headless substitute does not earn GUI credit, and the
profile cannot be narrowed after the run starts.

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
retain compact checksums of reviewed profile membership and exclusions. After
changing QA assets, also run `python -m pytest tests/qa/` as required by the
[authoring guide](user/AUTHORING.md).

## Current status

The repository contains the production definitions and validation tooling, but no
full qualification results. Execution still requires the reference native hosts,
provider access, authorized disposable resources, declared EDA provisioning, and
independent evidence storage. Record implementation progress in the relevant pull
request or issue, and put execution results in run records.

For the UART scenario, exact-a/VAL=32 timeout comparisons use the approved public
30 to 34 bit-time window in the [timing addendum](scenarios/uart/spec/timing-addendum.md).
Other timing observations remain blocked when the public contract cannot establish a
verdict.
