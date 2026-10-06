# Booley glossary map

Booley has seven bounded vocabularies. Read the shared glossary first, then only
the context that owns the work at hand.

## Contexts

| Context | Canonical glossary | Owns |
|---|---|---|
| Shared Booley | [docs/GLOSSARY.md](docs/GLOSSARY.md) | Product lifecycle, execution modes, Sandbox, Projects, Targets, Booley Flows, EDA provisioning, simulation evidence, and presentation |
| Goal Mode | [src/booley/goals/GLOSSARY.md](src/booley/goals/GLOSSARY.md) | Goal States, Worktree Identity, Goal Origins, Goal Changes, and Interrupted Applies inside one Goal Record |
| Ticket Board | [src/booley/ticket_board/GLOSSARY.md](src/booley/ticket_board/GLOSSARY.md) | Ticket authoring, Criteria, lifecycle, workspaces, acceptance, and escalation |
| B-Wave | [crates/bwave/GLOSSARY.md](crates/bwave/GLOSSARY.md) | Agent-facing waveform queries, virtual signals, markers, and human waveform viewing |
| Simulation Coverage | [src/booley/flows/sim/GLOSSARY.md](src/booley/flows/sim/GLOSSARY.md) | Coverage campaigns, measurement points, evaluation policy, waivers, and analysis |
| Feedback | [src/booley/feedback/GLOSSARY.md](src/booley/feedback/GLOSSARY.md) | Findings, friction, impressions, and their durable log |
| Public QA | [qa/GLOSSARY.md](qa/GLOSSARY.md) | QA Missions, Mission Areas, QA Runs, Release Smoke List, and Capability Map |

## Relationships

- **Ticket Board → Shared Booley**: Tickets select shared Targets and Booley
  Flows; Flow evidence satisfies the Ticket Board's Criteria.
- **Goal Mode → Shared Booley**: a Goal is judged by Booley Flow and Specialist
  evidence on shared Targets; the Goal Record holds that evidence.
- **Shared Booley → B-Wave**: a traced Simulation Flow produces a shared
  Trace Artifact, which B-Wave queries or opens in a Waveform Viewer.
- **Simulation Coverage → Shared Booley**: a coverage Campaign measures one
  shared Target through one Simulation Flow invocation.
- **Simulation Coverage → Ticket Board**: a Coverage Criterion contributes
  coverage evidence to a Ticket's acceptance state.
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
  QA's **Capability Map** is not a Simulation Coverage **Coverage Campaign**.
- Detailed behavior, schemas, commands, and implementation decisions belong in
  the relevant reference documentation rather than these glossaries.

System-wide decisions remain under [docs/adr/](docs/adr/). Context-specific ADR
directories should be created only when that context has a qualifying decision
to record.
