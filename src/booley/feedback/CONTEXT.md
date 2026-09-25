# Feedback glossary

This is the canonical vocabulary for observations captured while using or
qualifying Booley.

## Language

**Finding**:
One logged observation attributed to the project, Booley, documentation, QA
material, or an unresolved owner. Public QA classifies each Finding as `bug`
(product malfunction), `doc` (incorrect or missing documentation), `friction`
(difficulty without a demonstrated malfunction), `qa-bug` (a defect in the QA
mission or its inputs), or `wish` (a desired improvement). A Public QA Finding
also records severity, reproduction, expected and actual behavior, any
workaround, and local evidence.
_Avoid_: issue, Ticket, defect report

**Friction Report**:
A Feedback Finding that records confusion or difficulty without claiming a
malfunction. Public QA records the same observation with kind `friction`.
_Avoid_: minor bug, nitpick, UX bug

**Impression**:
A Finding that records subjective praise, a gripe, a wish, or mixed sentiment.
Public QA records an actionable desired improvement with kind `wish`; its
mission format does not collect general impressions.
_Avoid_: feature request, review, rating, testimonial

**Findings Log**:
The durable, append-only collection of Findings for one Project or Booley source checkout.
_Avoid_: bug database, feedback queue, telemetry
