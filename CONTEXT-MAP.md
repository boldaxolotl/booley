# Booley context map

Booley has six bounded vocabularies. Read the shared glossary first, then only
the context that owns the work at hand.

## Contexts

| Context | Canonical glossary | Owns |
|---|---|---|
| Shared Booley | [docs/CONTEXT.md](docs/CONTEXT.md) | Product lifecycle, execution modes, Session Runtime, Projects, Targets, Booley Flows, EDA provisioning, simulation evidence, and presentation |
| Ticket Board | [src/booley/ticket_board/CONTEXT.md](src/booley/ticket_board/CONTEXT.md) | Ticket authoring, Criteria, lifecycle, workspaces, acceptance, and escalation |
| B-Wave | [crates/bwave/CONTEXT.md](crates/bwave/CONTEXT.md) | Agent-facing waveform queries, virtual signals, markers, and human waveform viewing |
| Simulation Coverage | [src/booley/flows/sim/CONTEXT.md](src/booley/flows/sim/CONTEXT.md) | Coverage campaigns, measurement points, evaluation policy, waivers, and analysis |
| Feedback | [src/booley/feedback/CONTEXT.md](src/booley/feedback/CONTEXT.md) | Findings, friction, impressions, and their durable log |
| Public QA | [qa/CONTEXT.md](qa/CONTEXT.md) | Qualification Scenarios, Configured Scenarios, Scenario Runs, Checks, Observations, human triage, outcomes, and Capability Coverage |

## Relationships

- **Ticket Board → Shared Booley**: Tickets select shared Targets and Booley
  Flows; Flow evidence satisfies the Ticket Board's Criteria.
- **Shared Booley → B-Wave**: a traced Simulation Flow produces a shared
  Trace Artifact, which B-Wave queries or opens in a Waveform Viewer.
- **Simulation Coverage → Shared Booley**: a coverage Campaign measures one
  shared Target through one Simulation Flow invocation.
- **Simulation Coverage → Ticket Board**: a Coverage Criterion contributes
  coverage evidence to a Ticket's acceptance state.
- **Shared Booley and Public QA → Feedback**: product use records observations; Public
  QA human triage promotes confirmed product and documentation problems to Findings.
- **Public QA → all product contexts**: Scenario Checks qualify behavior
  across the other contexts. Its Capability Coverage is suite mapping, distinct
  from the RTL measurements owned by Simulation Coverage.

## Boundary rules

- Define each term in exactly one glossary. Other contexts link to that
  definition instead of restating it.
- Use the shared glossary for concepts that cross product capabilities. A
  context glossary may refer to shared terms with their shared meaning.
- An unqualified term means the definition owned by the current context. When
  writing across boundaries, qualify colliding terms such as **Scenario Run** and
  **Capability Coverage**.
- Detailed behavior, schemas, commands, and implementation decisions belong in
  the relevant reference documentation rather than these glossaries.

System-wide decisions remain under [docs/adr/](docs/adr/). Context-specific ADR
directories should be created only when that context has a qualifying decision
to record.
