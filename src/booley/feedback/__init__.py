"""Setup/port findings log, redaction, and local report export.

Three concerns, three modules:

- :mod:`findings` — the append-only log every setup step (and every sub-agent it
  delegates to) writes findings into. A file, not a conversation: a sub-agent's
  context dies on return, so anything it noticed has to be on disk before then.
- :mod:`redact` — deterministic, tested scrubbing of project identifiers. The
  redaction contract for a manually shared export is code, not a prompt: an
  agent promising "anonymized" is a promise nobody can verify.
- :mod:`render` — the persistent user report and explicitly exported redacted
  view for manual sharing.

The user-facing report is always produced; the Booley-facing view is rendered
on demand and persisted only by explicit export. See ``docs/user/CONFIG.md``
(``[feedback]``) for the redaction knobs.
"""

from __future__ import annotations
