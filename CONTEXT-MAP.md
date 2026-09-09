# Booley context map

Booley has four bounded vocabularies. Read the shared glossary first, then only
the context that owns the work at hand.

| Context | Canonical glossary | Owns |
|---|---|---|
| Shared Booley | [docs/CONTEXT.md](docs/CONTEXT.md) | Product lifecycle, execution modes, Session Runtime, Projects, Targets, Booley Flows, EDA provisioning, coverage, simulation evidence, presentation, and feedback |
| Ticket Board | [src/booley/ticket_board/CONTEXT.md](src/booley/ticket_board/CONTEXT.md) | Ticket authoring, Criteria, lifecycle, workspaces, acceptance, and escalation |
| B-Wave | [crates/bwave/CONTEXT.md](crates/bwave/CONTEXT.md) | Agent-facing waveform queries, virtual signals, markers, and human waveform viewing |
| Public QA | [qa/CONTEXT.md](qa/CONTEXT.md) | Qualification journeys, scenarios, profiles, checks, runs, results, and capability coverage |

## Boundary rules

- Define each term in exactly one glossary. Other contexts link to that
  definition instead of restating it.
- Use the shared glossary for concepts that cross product capabilities. A
  context glossary may refer to shared terms with their shared meaning.
- An unqualified term means the definition owned by the current context. When
  writing across boundaries, qualify colliding terms such as **QA Run** and
  **Capability Coverage**.
- Detailed behavior, schemas, commands, and implementation decisions belong in
  the relevant reference documentation rather than these glossaries.

System-wide decisions remain under [docs/adr/](docs/adr/). Context-specific ADR
directories should be created only when that context has a qualifying decision
to record.
