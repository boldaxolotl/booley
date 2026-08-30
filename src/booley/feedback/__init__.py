"""Setup/port findings log, redaction, and the opt-in Booley bug-report path.

Three concerns, three modules:

- :mod:`findings` — the append-only log every setup step (and every sub-agent it
  delegates to) writes findings into. A file, not a conversation: a sub-agent's
  context dies on return, so anything it noticed has to be on disk before then.
- :mod:`redact` — deterministic, tested scrubbing of project identifiers. The
  redaction contract on the path to a *public* issue is code, not a prompt: an
  agent promising "anonymized" is a promise nobody can verify.
- :mod:`render` / :mod:`submit` — the persistent user report, transient redacted
  outbound view, and consent-gated route to a GitHub issue or email hand-off.

The user-facing report is always produced; the Booley-facing view is rendered
on demand and persisted only by explicit export. See ``docs/user/CONFIG.md``
(``[feedback]``) for the knobs.
"""

from __future__ import annotations
