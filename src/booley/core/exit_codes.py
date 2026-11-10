"""Process exit codes that name a Flow, Specialist, or MCP endpoint verdict.

They live in ``booley.core`` so low-level presentation code can read a verdict
without depending on the runtime package.
"""

from __future__ import annotations

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_ERROR = 2
EXIT_CANCELLED = 130
