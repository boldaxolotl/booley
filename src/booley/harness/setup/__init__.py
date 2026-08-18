"""Setup stage: everything between ``booley run`` and the agent loop.

Ticket intake (parse, validate, atomically claim the ticket) followed by
workspace preparation (worktree + feature branch); all subsequent work is
handled by the criteria-based developer agent.
"""

from __future__ import annotations

from . import (
    intake as intake,
)
from . import (
    workspace as workspace,
)
