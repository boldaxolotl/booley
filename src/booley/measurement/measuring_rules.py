"""The rules for measuring work (Metric-Driven Development, #4133).

One text, shown wherever the rules appear: ``booley measure`` prints the rule of its exit
status, and the cheatsheet section ``booley cheat --metrics`` prints carries all of them. A
test keeps the cheatsheet in step with :data:`MEASURING_RULES`.
"""

from __future__ import annotations

from booley.core.exit_codes import EXIT_ERROR, EXIT_FAILURE

UNMET_RULE = (
    "Exit 1: fix, commit and measure again, up to 3 attempts unless the request "
    "states another number."
)
ERROR_RULE = "Exit 2: stop and report the error."

MEASURING_RULES: tuple[str, ...] = (
    "Measure only when a Measured Commit is asked for.",
    "Iterate with plain `booley flow`, not with `booley measure`.",
    "Measure the base at the start of a piece of work (`booley measure --all <base>`) only "
    "when the work has a relative target or a comparison is wanted.",
    UNMET_RULE,
    ERROR_RULE,
    "Never edit `metrics.toml` or `tests.toml` to get a met verdict, and never add tables, "
    "without approval.",
    "The work is complete when the commit is Measured with a green header.",
)


def exit_rule(exit_code: int) -> str | None:
    """The rule to follow after ``booley measure`` exits with *exit_code*; None when none applies."""
    if exit_code == EXIT_FAILURE:
        return UNMET_RULE
    if exit_code == EXIT_ERROR:
        return ERROR_RULE
    return None
