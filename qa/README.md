# Public Booley QA suite

The production assets encode the accepted three journeys and explicit qualification
profiles. They are not qualification evidence. No full scenario or required profile
has been executed by this implementation work.

| Journey | Production scenario | Accepted design |
|---|---|---|
| PicoRV32 published-demo continuity and evolution | [scenario.yaml](scenarios/picorv32/scenario.yaml) | [#374](https://github.com/boldaxolotl/booley/issues/374) |
| Taxi 10G MAC port and evolution | [scenario.yaml](scenarios/taxi/scenario.yaml) | [#377](https://github.com/boldaxolotl/booley/issues/377) |
| Documentation-only standalone UART | [scenario.yaml](scenarios/uart/scenario.yaml) | [#375](https://github.com/boldaxolotl/booley/issues/375) |

[Profiles](profiles.yaml) select complete per-run check lists; [coverage](coverage.yaml)
contains the 62 product capabilities and 16 distinct EDA integration references.
The 1,072 authored checks preserve the reviewed journey, supplemental and companion
identities. These counts establish representation, not behavioral sufficiency.

```sh
python -m pip install -r qa/requirements-validation.txt
python qa/validate.py
python qa/validate.py --scenario picorv32-published-demo-continuity
python qa/validate.py --coverage-index /tmp/booley-qa-coverage.json
```

Validation checks structure, references, explicit selections, asset digests, ordered
prerequisites, fault-recovery reachability and budgets. It executes no scenario.
The authoring filter cannot produce a whole-suite coverage index.

Read [Protocol](PROTOCOL.md), [Qualification](QUALIFICATION.md),
[Format](FORMAT.md), [Authoring](AUTHORING.md) and the [worked example](examples/README.md).
The [reviewed handoff](HANDOFF.md) and its migration records retain decision history.
The [implementation status](IMPLEMENTATION.md) lists tested changes and unfinished
work. In particular, supplemental fault injection infrastructure and parts of the
[UART evaluator](scenarios/uart/evaluator/README.md) remain incomplete.

Execution requires the exact released Booley package/image and matching docs,
reference native hosts, authorized disposable resources, provider access, declared
EDA provisioning, and independent operator evidence storage. GUI profiles require
actual supported VS Code clients, WCP and a qualified screenshot observer. Missing
infrastructure leaves the corresponding profiles incomplete; no headless substitute
or smaller profile is implied. There is no generic runner or unattended campaign here.
