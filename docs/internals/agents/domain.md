# Domain docs

Where Booley's domain vocabulary and design decisions live, and how agents
use them. Skills that explore or change the codebase (`triage`, `tdd`,
`diagnosing-bugs`, `improve-codebase-architecture`) read it before they name
concepts in issues, specs, code, or tests. How to write glossary entries and
ADRs is the `domain-modeling` skill's job.

## Before exploring

1. Read [GLOSSARY-MAP.md](../../../GLOSSARY-MAP.md). Booley has several
   bounded contexts, and the map says which glossary owns each concept.
2. Read [docs/GLOSSARY.md](../../GLOSSARY.md), then every context glossary
   whose concepts the work touches.
3. Read the ADRs under [docs/adr/](../../adr/) that cover the area.

## Use the glossary's words

Name domain concepts exactly as the owning glossary defines them, in issue
titles, briefs, specs, code, tests, and proposals. Each entry's `_Avoid_`
field lists the words to leave out. A term means its owning context's
definition; qualify it when you write across contexts.

When a concept you need has no entry, either the project does not use that
concept, so rethink the wording, or the glossary has a gap: record it for
`domain-modeling` instead of inventing a term.

## Flag ADR conflicts

When proposed work contradicts an ADR, say so and name the ADR, so the
maintainer can decide whether to reopen it. An ADR records why a decision was
made; overriding it silently loses that reasoning.
