# Booley glossary map

This map lists Booley's six canonical glossaries and the rules that keep them
apart. It is for contributors and agents who need a term's meaning or are
adding one. Read the shared glossary first, then only the context that owns
the work at hand.

## Contexts

| Context | Canonical glossary | Owns |
|---|---|---|
| Shared Booley | [docs/GLOSSARY.md](docs/GLOSSARY.md) | Product lifecycle, execution modes, Sandbox, Jobs, Goals and Goal Changes, Projects, Booley Directory and Project Root, Stealth, Targets, Booley Flows and Specialists, EDA provisioning, simulation evidence, Metric-Driven Development, and presentation |
| Goal Mode | [src/booley/goals/GLOSSARY.md](src/booley/goals/GLOSSARY.md) | Worktree Identity, Run Binding, and Frozen Test Runs inside one Goal Record; retires with Goal Mode in 0.5.0 |
| B-Wave | [crates/bwave/GLOSSARY.md](crates/bwave/GLOSSARY.md) | Agent-facing waveform queries, virtual signals, markers, and human waveform viewing |
| Simulation Coverage | [src/booley/flows/sim/GLOSSARY.md](src/booley/flows/sim/GLOSSARY.md) | Coverage Records, measurement points, evaluation policy, waivers, and analysis |
| Feedback | [src/booley/feedback/GLOSSARY.md](src/booley/feedback/GLOSSARY.md) | Findings, their kinds, and their durable log |
| Public QA | [qa/GLOSSARY.md](qa/GLOSSARY.md) | Missions, Mission Areas, QA Runs, Operator, Release Smoke List, and Capability Map |

## Relationships

- **Goal Mode → Shared Booley**: a Goal is judged by Booley Flow and Specialist
  evidence on shared Targets; the Goal Record holds that evidence.
- **Shared Booley → B-Wave**: a traced Simulation Flow produces a shared
  Trace Artifact, which B-Wave queries or opens in a Waveform Viewer.
- **Simulation Coverage → Shared Booley**: a Coverage Record measures one
  shared Target through one Simulation Flow invocation; a Simulation Record may
  contain a Coverage Record.
- **Simulation Coverage → Goal Mode**: a Coverage Criterion supplies the
  evaluation policy for a coverage Goal; its evidence is stored in the Goal Record.
- **Shared Booley and Public QA → Feedback**: product use records observations; Public
  QA triage files confirmed product and documentation problems as issues.
- **Public QA → all product contexts**: QA missions hunt for bugs across the
  other contexts. Its capability map (`qa/AREAS.md`) is suite mapping, distinct
  from the RTL measurements owned by Simulation Coverage.

## Boundary rules

- Define each term in exactly one glossary. Other contexts link to that
  definition instead of restating it.
- Use the shared glossary for concepts that cross product capabilities. A
  context glossary may refer to shared terms with their shared meaning.
- An unqualified term means the definition owned by the current context. When
  writing across boundaries, qualify colliding concepts; for example, Public
  QA's **Capability Map** is not a Simulation Coverage **Coverage Record**.
- Detailed behavior, schemas, commands, and implementation decisions belong in
  the relevant reference documentation rather than these glossaries.
- The `booley-setup` skill ships a plain-language companion,
  [its GLOSSARY.md](src/booley/data/skills/booley-setup/GLOSSARY.md), that
  restates shared terms for users during Project Setup. It is not canonical:
  when a shared definition changes, update the companion to match.

System-wide decisions remain under [docs/adr/](docs/adr/). Context-specific ADR
directories should be created only when that context has a qualifying decision
to record.
