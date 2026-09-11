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

Agents perform setup, product exercises, fault injection, recovery, evidence
collection, and safe shutdown and cleanup of run-owned resources. The Human Maintainer
still owns the Scenarios, required Configured Scenarios, acceptance rules, and final
qualification decision.

## Starting a run

Explicitly invoke the user-only [`booley-qa-run` skill](booley-qa-run/SKILL.md) with a
Scenario ID, a Configured Scenario ID, and an artifact root. The agent never invokes
it on its own.

The selected product may be an existing immutable artifact or an exact source commit.
For an exact commit, admission may create or refresh its verified candidate wheel,
install it in an operator-only host environment, and reconcile `booley bootstrap`
before the Scenario Run is frozen. Scenario Checks still exercise their own declared
installation and Host Bootstrap behavior.

QA execution is an agent skill, not a Booley CLI command. The agent reading the skill
is the Scenario Operator. The [protocol](doc/PROTOCOL.md) discloses one resumable
stage at a time and owns execution behavior. `validate.py` checks authored QA assets
only; it does not execute a Scenario Run.

## Adding behavior to QA

Explicitly invoke the user-only [`booley-add-to-qa` skill](booley-add-to-qa/SKILL.md)
with prose describing the proposed public behavior. It identifies existing coverage or
proposes new Capabilities and Checks for Human Maintainer approval, then validates the
approved changes. It does not execute Scenario Runs.

## What's in this directory

| Path | Contents |
|---|---|
| [`scenarios/`](scenarios/) | Production scenario YAML plus each Scenario's prompts, Ticket payloads, fixtures, specifications, and evaluator material |
| [`shared/coverage/`](shared/coverage/RUNBOOK.md) | Fixed native coverage fixtures, expected operands, approved waiver inputs, independent evaluator and external boundary controls |
| [`coverage.yaml`](coverage.yaml) | Product capability inventory and public contract sources |
| [`scenario.schema.json`](scenario.schema.json) | Structural contract for scenario files |
| [`validate.py`](validate.py) | Offline validation of structure, references, Configured Scenarios, asset hashes, prerequisites, fault recovery, budgets, and coverage |
| [`booley-add-to-qa/`](booley-add-to-qa/) | Explicitly invoked skill that turns a proposed public behavior into reviewed Capability and Check changes |
| [`booley-qa-run/`](booley-qa-run/) | Skill that coordinates an evidence-producing Scenario Run |
| [`doc/`](doc/) | Execution protocol, run record contract, and qualification rules |

The current suite maps 69 product capabilities and 16 distinct EDA integration
references to 1,658 checks. Those counts show that the reviewed requirements are
represented. They do not prove that Booley passes them.

## Scenario and Check structure

[`scenario.schema.json`](scenario.schema.json) is the structural authority. Scenario
files use `format_version: 1`.

A Scenario declares:

- its identity, source, inputs, and public authorities;
- named Check sets and Configured Scenarios;
- a shared time budget and ordered phases; and
- ordered Steps.

Each Step declares:

- its identity, phase, action, and Checks; and
- any prerequisites, authority, timeout, retry, recovery, resources, and assets.

A Configured Scenario declares:

- whether it is required;
- its host, client, and backend parameters;
- its pre-run requirements and selected Check sets; and
- any justified exclusions.

Each Check is one independently observable product claim declaring:

- its ID and Capability references;
- its stimulus and expected result;
- the public authority for that expectation; and
- its required evidence, capture location, and any navigation-only references.

## Scenarios

| Scenario | What it exercises | Sources |
|---|---|---|
| PicoRV32 published demo continuity and evolution | Starts from the pinned published-demo Project and upstream source. It checks clean simulation, lint, synthesis, license-free Vivado ML Standard execution on provisioned Linux, Interactive Mode and waveform diagnosis, two Ticket Mode changes, final regression, and cleanup. Paid-license policy and relay checks remain in a separate optional Configured Scenario. | [Scenario](scenarios/picorv32/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/374) |
| Taxi 10G MAC port and evolution | Starts from a pinned direct clone of Taxi. It checks Project setup, the clean 10G MAC baseline, FST and B-Wave behavior, a disposable submodule companion Project, mutation testing, two Ticket Mode changes, final regression, and cleanup. | [Scenario](scenarios/taxi/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/377) |
| Documentation-only standalone UART | Builds a UART from the allowlisted OpenTitan documentation corpus without giving the developer the reference implementation or oracle. It covers Interactive Mode, feature and repair Tickets, independent evaluator controls and cases, external-image handling, final regression, and cleanup. | [Scenario](scenarios/uart/scenario.yaml), [accepted design](https://github.com/boldaxolotl/booley/issues/375), [oracle contract](scenarios/uart/evaluator/CONTRACT.md) |

| Native coverage: measurement | Collection opt-in, harnesses, staging, native windows and exact directional toggle/value-property counts. | [Scenario](scenarios/coverage-measurement/scenario.yaml), [fixture runbook](shared/coverage/RUNBOOK.md) |
| Native coverage: policy | Metric policies, exact thresholds, independent simulation/coverage verdicts and approved waivers. | [Scenario](scenarios/coverage-policy/scenario.yaml), [fixture runbook](shared/coverage/RUNBOOK.md) |
| Native coverage: storage | V3 integrity, per-source aggregation, corruption rejection and large Campaigns. | [Scenario](scenarios/coverage-storage/scenario.yaml), [fixture runbook](shared/coverage/RUNBOOK.md) |
| Native coverage: lifecycle | Durable publication, interrupted/restarted producers, Ticket gap closure and exact retention. | [Scenario](scenarios/coverage-lifecycle/scenario.yaml), [fixture runbook](shared/coverage/RUNBOOK.md) |
| Native coverage: analysis | Real Coverage Analyst advice, source closure modes, exact-path validation and provider isolation. | [Scenario](scenarios/coverage-analysis/scenario.yaml), [fixture runbook](shared/coverage/RUNBOOK.md) |
| Native coverage: boundary | Bounded evidence queries, candidate screening, delivered references and controlled model failures. | [Scenario](scenarios/coverage-boundary/scenario.yaml), [fixture runbook](shared/coverage/RUNBOOK.md) |

The six coverage Scenarios each require Ubuntu and Windows with both Codex and
Claude native clients: 24 additional Configured Scenario Runs. The fixtures pin
Verilator 5.052 and exact arithmetic; authoring validation and fixture self-tests
do not qualify these runs. The three existing Scenarios retain their memberships.

The scenarios retain their reviewed pins, workloads, thresholds, prompts, authority,
fault and recovery sequences, independent evaluation, and product cleanup Checks. The
[published design](https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/HANDOFF.md)
records the historical decisions behind the production files.

## Validate changes

After changing QA assets, run:

```sh
python qa/validate.py
python -m pytest tests/qa/
```
