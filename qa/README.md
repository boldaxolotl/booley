# Booley QA

This is the index of Booley's maintainer QA suite, for the maintainer and the
agents who run it: what each mission covers, how to install the QA skills, and
where the other QA files live. QA terms such as Mission, Mission Area, and QA
Run are defined in [GLOSSARY.md](GLOSSARY.md). Booley's own automated tests
are in [CONTRIBUTING.md](../docs/internals/CONTRIBUTING.md).

Booley has one maintainer and no human QA team, so agents do the QA. Each QA
Run is a **timeboxed bug hunt**: an agent, the operator, takes a candidate
Booley build through a realistic journey on a pinned open-source IP and writes
down everything that breaks, confuses, or slows it down. The metric is real
findings per run.

Missions describe where to look, not a checklist to certify. The picorv32
mission goes furthest: after a short baseline it is a free hunt guided only by
the user docs, looking for the bugs the planned missions don't look for. When something
fails, the operator records it, works around it, and keeps going, so one broken
step never hides the bugs behind it.

## Missions

| Mission | Journey |
|---|---|
| [picorv32](missions/picorv32/MISSION.md) | Free hunt on the published demo Project: Quick start, baseline and Stealth guard, then anywhere the user docs lead, with optional seeded faults and Goals as bait |
| [taxi](missions/taxi/MISSION.md) | Port of the Taxi 10G MAC from a direct clone: setup, baseline, FST and B-Wave, submodule companion Project, mutation testing, two Goals, campaign, Vivado, cleanup |
| [uart](missions/uart/MISSION.md) | Clean-room UART from the OpenTitan documentation with an independent evaluator; also crash resume, simulation grading and Project discovery |
| [coverage](missions/coverage/MISSION.md) | Native coverage on a fixed fixture Project with known counts: collection, arithmetic, policy, waivers, storage, retention, a coverage Goal, the Coverage Analyst |
| [mutation](missions/mutation/MISSION.md) | Mutation Flow on a fixture Project with a hand-written list of every Mutant: sites and decoys, determinism, selection, scoring, hangs, bad input, removed surface |
| [gui](missions/gui/MISSION.md) | Browser Codespace on the picorv32 demo with a candidate image: onboarding, Dashboard under a live Goal, B-Wave scoped views, stop and resume, terminal output, Review Package, themes, docs site, all checked against the CLI |

Each mission's primary areas fit an 8-hour budget (`gui`: 4 hours) and run in
priority order; areas marked optional follow-up sit outside that budget. Host
and client coverage (Windows, other agent clients) comes from rerunning a
mission on that host or client.

## Skills

Invoke these explicitly; agents do not start them on their own.

Install them from a primary Booley source checkout, from any directory inside
it:

```bash
booley bootstrap --with-qa-skills
```

- **Where they go:** links into Codex's `~/.agents/skills` and, when
  `~/.claude` exists as a separate directory, `~/.claude/skills`. A linked
  worktree is refused because its links would dangle once it is removed. A
  dirty tree, another branch, or a different installed Booley version is
  accepted here; each skill checks its checkout when it runs.
- **Staying installed:** the opt-in is recorded in
  `~/.agents/booley-qa-skills.json`, and plain `booley bootstrap` relinks from
  the recorded path. `booley bootstrap --check-only`, Doctor, and
  `booley init` only inspect. A missing checkout or skill is a warning with
  recovery steps, never a failure, except that `--with-qa-skills` and
  `--without-qa-skills` exit 2 when the QA step warns.
- **Moving the checkout:** rerun `booley bootstrap --with-qa-skills --force`
  from its new location.
- **Removing them:** `booley bootstrap --without-qa-skills` deletes only the QA
  links Booley recorded, even when the opt-in file is corrupt. Product skills
  and hand-made or replaced entries stay. A QA-named link that Booley did not
  record is a conflict even with `--force`; `--force` relinks only recorded
  links.
- **Host only:** QA skills drive the maintainer's host and repository, so they
  stay out of release wheels and out of the Sandbox's host-skill mounts.
- **Explicit only:** `disable-model-invocation: true` in each `SKILL.md`
  stops Claude Code from starting it on its own, and each skill's
  `agents/openai.yaml` does the same in Codex. Other clients get the rule only
  as an instruction in `SKILL.md`, which is guidance, not a security boundary.

| Skill | Use |
|---|---|
| [`booley-qa-run`](booley-qa-run/SKILL.md) | Run one mission against a Booley build; writes `findings.md`, `log.md`, `resources.md`, `evidence/` |
| [`booley-qa-triage`](booley-qa-triage/SKILL.md) | Triage findings: interactively with the maintainer one cluster at a time, or automatically by a unanimous model jury (`booley-qa-run` does this by default); verify against current main, then file issues or fix the mission |
| [`booley-add-to-qa`](booley-add-to-qa/SKILL.md) | Add a behavior, fault idea, or known trap to a mission |
| [`booley-gui-check`](booley-gui-check/SKILL.md) | Check one PR's GUI change in a Codespace on its candidate image; posts a screenshot table on the PR and returns pass, fail, or blocked per step |

## Other files

| Path | Contents |
|---|---|
| [`GLOSSARY.md`](GLOSSARY.md) | QA vocabulary: Mission, Mission Area, QA Run, Operator, Release Smoke List, Capability Map |
| [`SMOKE.md`](SMOKE.md) | Ten must-pass release items, run with `booley-qa-run ... smoke` |
| [`DISK.md`](DISK.md) | Disk preflight every QA Run performs first |
| [`AREAS.md`](AREAS.md) | Booley capabilities mapped to the mission areas that exercise them |
| [`missions/`](missions/) | Mission files plus their prompts, Goalsets and ad-hoc Goal lists, fixtures, specs, and evaluators |
| [`shared/GOAL-CHILD.md`](shared/GOAL-CHILD.md) | Rules for every mission area that runs a Goal child |
| [`shared/coverage/`](shared/coverage/RUNBOOK.md) | Native-coverage fixture Project, expected values, fault injectors, and evaluator |
| [`shared/mutation/`](shared/mutation/RUNBOOK.md) | Mutation fixture Project, hand-written expected Mutants and outcomes, and fault files; used by the `mutation` mission |
| [`shared/codespaces/`](shared/codespaces/RUNBOOK.md) | Browser-driven Codespace on the picorv32 demo with a candidate image: setup, reading the terminal, GUI-vs-CLI oracles, evidence, cleanup, candidate image deletion; used by the `gui` mission and `booley-gui-check` |

## Checking changes

```sh
python -m pytest tests/qa/
```
