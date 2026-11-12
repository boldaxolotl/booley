# Scheduled whole-module mutation testing of Booley's Python code

Booley does not run scheduled mutation testing over its own Python code.
The weekly mutmut campaign over the harness setup package was removed
(#3404).

This does not concern Booley's own mutation Goal family or the Mutation
Flow, which test users' RTL.

## Why this is out of scope

- Nobody consumed the results.
- Finishing a campaign takes about 22-30 CPU-hours a week. It could not
  finish inside its 35-minute budget (#2970), so the counts were partial and
  biased toward cheap mutants.
- Most survivors were noise.

## When to reconsider

Mutation testing of a PR's changed code, or a one-off run on a single
high-stakes module, is open for a future proposal.

## Prior requests

- #2970: scheduled mutmut job overran its time budget, which exposed
  the cost
- #3404: removal of the campaign
