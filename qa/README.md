# Public Booley QA suite

The production assets encode the accepted three journeys and explicit qualification
profiles. Qualification requires recorded execution evidence for the selected profiles.

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
The [published design](https://github.com/boldaxolotl/booley/blob/b163fd1f45b76f3950005678e500e695232832fb/qa/HANDOFF.md)
preserves historical decisions. Current contracts live in the scenario assets,
[format](FORMAT.md), [profiles](profiles.yaml), and
[UART oracle derivation](scenarios/uart/evaluator/CONTRACT.md). Regression tests
retain compact checksums of the reviewed profile membership and exclusions.

UART timeout comparisons remain blocked where the public timing contract cannot
establish a verdict. Implementation progress and run results belong in the PR,
issues, and run records rather than this reference.

Execution requires the exact released Booley package/image and matching docs,
reference native hosts, authorized disposable resources, provider access, declared
EDA provisioning, and independent operator evidence storage. GUI profiles require
actual supported VS Code clients, WCP and a qualified screenshot observer. Missing
infrastructure leaves the corresponding profiles incomplete; no headless substitute
or smaller profile is implied. There is no generic runner or unattended campaign here.
