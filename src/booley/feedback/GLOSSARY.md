# Feedback glossary

This is the canonical vocabulary for observations captured while using or
qualifying Booley, for contributors and agents who log, triage, or export
them. Public QA's own terms are in the [Public QA glossary](../../../qa/GLOSSARY.md).

## Language

**Finding**:
One logged observation attributed to the project, Booley, documentation, QA
material, or an unresolved owner. It is a problem or one of three other
kinds: a **Friction Report**, confusion or difficulty without a claimed
malfunction; an **Impression**, subjective praise, a gripe, a wish, or mixed
sentiment; or a **Win**, a check that passed on the first try, logged so that
a count of problems can be read against how much was exercised. Public QA
classifies each Finding as `bug` (product malfunction), `doc` (incorrect or
missing documentation), `friction` (difficulty without a demonstrated
malfunction), `qa-bug` (a defect in the QA mission or its inputs), or `wish`
(a desired improvement); its missions collect no general Impressions. A
Public QA Finding also records severity, reproduction, expected and actual
behavior, any workaround, and local evidence.
_Avoid_: issue, Ticket, defect report; minor bug, nitpick, UX bug (for a Friction Report); feature request, rating, testimonial (for an Impression); success, pass (for a Win)

**Findings Log**:
The durable collection of Findings for one Project or Booley source checkout.
Findings are added and never removed; triage and filing update them in place.
_Avoid_: bug database, feedback queue, telemetry
