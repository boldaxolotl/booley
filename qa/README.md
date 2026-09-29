# Booley QA

Booley has one maintainer and no human QA team, so agents do the QA. Each QA run
is a **timeboxed bug hunt**: an agent takes a candidate Booley build through a
realistic journey on a pinned open-source IP and writes down everything that
breaks, confuses, or slows it down. The metric is real findings per run.

Missions describe where to look, not a checklist to certify. When something
fails, the operator records it, works around it, and keeps going, so one broken
step never hides the bugs behind it.

## Missions

| Mission | Journey |
|---|---|
| [picorv32](missions/picorv32/MISSION.md) | Published demo Project: baseline sim/lint/synth, Vivado, Interactive Mode and waveform diagnosis, two Ticket Mode changes, blocked-Ticket amendment, simulation campaign, cleanup |
| [taxi](missions/taxi/MISSION.md) | Port of the Taxi 10G MAC from a direct clone: setup, baseline, FST and B-Wave, submodule companion Project, mutation testing, two Tickets, campaign, cleanup |
| [uart](missions/uart/MISSION.md) | Clean-room UART from the OpenTitan documentation with an independent evaluator |
| [coverage](missions/coverage/MISSION.md) | Native coverage on a fixed fixture Project with known counts: collection, arithmetic, policy, waivers, storage, retention, a coverage Ticket, the Coverage Analyst |

Each mission fits an 8-hour budget with areas in priority order. Host and client
coverage (Windows, other agent clients) comes from rerunning a mission on that
host or client.

## Skills

Invoke these explicitly; agents do not start them on their own.

From a complete, clean primary Booley checkout on `main` whose HEAD matches the
canonical host-installed Booley revision, a maintainer can persistently install
all three into Codex and an existing distinct Claude skill target:

```bash
booley bootstrap --with-qa-skills
```

Plain Bootstrap and Project Initialization then keep them reconciled from this
live checkout. Remove only Booley-owned QA links with
`booley bootstrap --without-qa-skills`. A moved, deleted, dirty, off-main, or
revision-skewed checkout produces a non-blocking warning with recovery guidance.
They are host-only maintainer tools and are excluded from Sandbox host-skill
mounts. Codex metadata disables implicit invocation; other clients receive the
same explicit-only instruction as behavioral guidance, not a security boundary.

| Skill | Use |
|---|---|
| [`booley-qa-run`](booley-qa-run/SKILL.md) | Run one mission against a Booley build; writes `findings.md`, `log.md`, `resources.md`, `evidence/` |
| [`booley-qa-triage`](booley-qa-triage/SKILL.md) | Triage findings with the maintainer one cluster at a time: verify against current main, decide, then file issues or fix the mission |
| [`booley-add-to-qa`](booley-add-to-qa/SKILL.md) | Add a behavior, fault idea, or known trap to a mission |

## Other files

| Path | Contents |
|---|---|
| [`SMOKE.md`](SMOKE.md) | Ten must-pass release items, run with `booley-qa-run ... smoke` |
| [`DISK.md`](DISK.md) | Disk preflight every run performs first |
| [`AREAS.md`](AREAS.md) | Booley capabilities mapped to the mission areas that exercise them |
| [`missions/`](missions/) | Mission files plus their prompts, Ticket payloads, fixtures, specs, and evaluators |
| [`shared/coverage/`](shared/coverage/RUNBOOK.md) | Native-coverage fixture Project, expected values, fault injectors, and evaluator |

## Checking changes

```sh
python -m pytest tests/qa/
```
