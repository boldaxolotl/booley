# Rename Simulation and Coverage Campaigns to Records

Status: accepted (2026-11-06/07 triage of
[#2711](https://github.com/boldaxolotl/booley/issues/2711))

Supersedes the campaign terminology of
[ADR 0062](0062-split-coverage-campaign-summary-from-point-storage.md),
[ADR 0064](0064-make-simulation-campaign-the-resume-authority.md) and
[ADR 0072](0072-declare-test-variants-and-seeds-instead-of-run-overrides.md).
Their other decisions stand.

## Context

In verification, a campaign is a large planned effort: many regressions, or
coverage closure over days or weeks. Booley used the word for much smaller
things. A **Simulation Campaign** was the durable, resumable record of the
workload one Simulation Flow invocation runs on one Target, and a **Coverage
Campaign** was the native RTL coverage record for one Target in one Simulation
Flow invocation. Engineers who hear "campaign" expect something that spans many
runs. The two terms also collided: a coverage-collecting Simulation Campaign
may contain a Coverage Campaign, so the glossaries and `GLOSSARY-MAP.md` had to
warn that the two are distinct.

## Decision

1. **Names.** Simulation Campaign becomes **Simulation Record**; Coverage
   Campaign becomes **Coverage Record**. A Simulation Record may contain a
   Coverage Record. "Campaign" stays only for mutation testing: the **Mutation
   Campaign** and the Target campaign that feeds it.
2. **Full rename.** Glossaries, docs, messages, persisted schema IDs, document
   keys, on-disk paths, reason codes, output fields, inputs, and internal
   identifiers all move to the new names. The existing lowercase "coverage
   record" uses in code (the Criteria coverage template item and the Verilator
   native counter record) are renamed too, so the term is unambiguous.
3. **Old records are refused.** Records with the pre-rename schema IDs, as
   written by 0.4.x and earlier, are refused with a clear error that names the
   new format, as 0.3.0 records were. There is no dual-read.
4. **No aliases.** Old reason codes, output fields, the `{campaign}` `run_cwd`
   placeholder and the `coverage_analyst` `campaign` input are replaced
   outright. The CHANGELOG calls the rename a breaking change.
5. **Goal-side names wait for the Sign-off redesign.** `booley.goal-campaign`,
   `unsettled_campaigns`, `goal-campaign-unaccounted`,
   `logs/acceptance/campaigns/`, and the `campaign` criterion freshness
   category (to become `test-selection`) are renamed by
   [#2811](https://github.com/boldaxolotl/booley/issues/2811), not by #2711.
6. **The code and contract rename waits.** It starts after the Simulation Flow
   simplification work
   ([#1689](https://github.com/boldaxolotl/booley/issues/1689),
   [#1742](https://github.com/boldaxolotl/booley/issues/1742),
   [#2307](https://github.com/boldaxolotl/booley/issues/2307),
   [#1295](https://github.com/boldaxolotl/booley/issues/1295)) has landed, so
   the two do not conflict.

Delivery:
[#2908](https://github.com/boldaxolotl/booley/issues/2908) (glossaries and this
ADR),
[#2909](https://github.com/boldaxolotl/booley/issues/2909) (persisted formats,
codes and fields; refusal of old records),
[#2910](https://github.com/boldaxolotl/booley/issues/2910) (identifiers,
modules and messages), and
[#2911](https://github.com/boldaxolotl/booley/issues/2911) (docs and
CHANGELOG).

## Rejected alternatives

- **Coverage Collection** for the coverage term. It names the act, not the
  artifact, and collides with the generic phrase "coverage collection" that
  user docs already use.
- **Keeping "campaign".** It overstates both terms and keeps the
  campaign-in-campaign collision.
- **Dual-read of 0.4.x records and one-release aliases** for old codes, fields
  and inputs. They add compatibility code to maintain for a rename; refusal
  with a clear error has precedent in the 0.3.0 format change.
- **Renaming before the Simulation Flow simplification work.** A tree-wide
  rename in the middle of that work would cause heavy conflicts.

## Consequences

- Until #2909, #2910 and #2911 land, code, messages, persisted formats, and
  docs outside the glossaries still say "campaign". The glossaries carry a
  transition note saying so.
- Earlier ADRs keep their wording as historical records. This mapping applies
  to every earlier ADR, not only the three that carry a note: ADRs 0058
  (simulation adapter seam), 0063, 0066, 0075, 0086 and 0087 (admit jobs) also
  use the old words.
- Interrupted runs and stored coverage from 0.4.x cannot be resumed or loaded
  after the contract rename; users rerun them.
- Mutation Campaign is the only defined glossary term that says "campaign".
