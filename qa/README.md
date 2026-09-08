# Public QA suite design

This directory records the simplified shared contract for
[Wayfinder #246](https://github.com/boldaxolotl/booley/issues/246).
The three accepted journeys and their workloads remain unchanged.

- [Protocol](PROTOCOL.md): executing a run and retaining trustworthy results.
- [Qualification](QUALIFICATION.md): required runs, coverage, and scoped verdicts.
- [Format](FORMAT.md): scenario and run files, identities, and validation.
- [Authoring](AUTHORING.md): adding and revising checks.
- [Handoff](HANDOFF.md): remaining work in
  [#376](https://github.com/boldaxolotl/booley/issues/376).
- [Worked example](examples/README.md): normal, fault/recovery, and unavailable
  GUI checks with illustrative run records.

This is a design handoff, not an executable suite. Production scenario YAML,
coverage inventory, profile files, schema/validator, independent UART evaluator,
and execution tooling are not supplied here. Examples confer no coverage credit.

The accepted journey designs remain:

| Journey | Design |
|---|---|
| PicoRV32 published-demo continuity and evolution | [#374](https://github.com/boldaxolotl/booley/issues/374) |
| Taxi 10G MAC port and evolution | [#377](https://github.com/boldaxolotl/booley/issues/377) |
| OpenTitan UART clean-room greenfield | [#375](https://github.com/boldaxolotl/booley/issues/375) |

These shared documents replace the earlier bookkeeping, promotion, format, and
qualification requirements identified in the issue updates. Journey-specific pins,
stimuli, thresholds, authority, independent evaluation, and cleanup remain binding.
