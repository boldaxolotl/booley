# Domain docs

Booley is a multiple-context repository. `CONTEXT-MAP.md` is the authoritative
index of context ownership.

## Before exploring

- Read `CONTEXT-MAP.md`, then `docs/CONTEXT.md` and every context glossary whose
  owned concepts the work touches.
- Read relevant ADRs under `docs/adr/` when that directory exists.
- If an expected document does not exist, proceed silently. Domain-modeling
  creates documentation lazily when terminology or durable decisions are
  resolved.

## Use the glossary vocabulary

Use terms exactly as defined by the owning glossary in `CONTEXT-MAP.md` when
naming domain concepts in issues, specifications, code, tests, hypotheses, and
architectural proposals.

Do not substitute words listed in an entry's `_Avoid_` field.

If a required concept is absent, either reconsider whether the project uses
that concept or record the gap for `domain-modeling`.

## Flag ADR conflicts

If proposed work contradicts an existing ADR, surface the conflict explicitly
rather than silently overriding the decision.
