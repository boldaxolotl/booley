"""The Run wizard's hand-made form for the ``reviewer`` Specialist (#3816).

No Textual here. Flow forms are generated from a parser (``run_form``); a
Specialist's form is written by hand, because a person starts a review with a
category, a focus and a scope rather than with every parser option. The options
the form leaves out are named in the drift guard's test, so a Specialist that
gains an option fails CI until someone places it or hides it with a reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from booley.goals.model import REVIEW_KINDS
from booley.harness.dashboard.run_form import Field, text_words

# The one Specialist the wizard has a form for.
REVIEWER: Final = "reviewer"
FORM_NOT_BUILT: Final = "form not built yet"

CATEGORIES: Final = ("rtl", "tb")
_SPEC_FOCUS: Final = "spec"

_TYPED = "Typed as one value."


def _text(dest: str, label: str, flag: str, hint: str) -> Field:
    """A text row writing ``flag value``."""
    return Field(dest, "text", label, flag, f"{hint} {_TYPED}", (flag,))


_CATEGORY = Field(
    "category",
    "list",
    "Category",
    "--category",
    "rtl reviews design sources, tb reviews testbench sources.",
    ("--category",),
    choices=CATEGORIES,
    required=True,
)
_SPEC = _text(
    "spec", "Spec path", "--spec", "The specification the review checks, a worktree path."
)
_SCOPE = _text("scope", "Scope", "--scope", "Comma-separated files to review (required).")
_INSTRUCTION = _text("instruction", "Instruction", "--instruction", "Extra instructions.")
_MORE = Field("more", "more", "More", "", "Enter shows or hides max turns and timeout.", ())
_MAX_TURNS = _text("max_turns", "Max turns", "--max-turns", "Most conversation turns.")
_TIMEOUT = _text("timeout", "Timeout", "--timeout", "How long the review may run, e.g. 30m.")


def focuses(category: str) -> tuple[str, ...]:
    """The review focuses of *category*: the review Goal kinds, so a new Goal kind appears here."""
    prefix = f"{category}_"
    return tuple(kind.removeprefix(prefix) for kind in REVIEW_KINDS if kind.startswith(prefix))


@dataclass(frozen=True)
class ReviewerForm:
    """The reviewer's form: its rows follow the values, and the values become the command's words.

    ``spec_prefill`` is the spec the worktree's active spec-review Goal names (else empty).
    """

    spec_prefill: str

    def initial(self) -> dict[str, str]:
        """The values a fresh form starts with."""
        return {"category": "rtl", "spec": self.spec_prefill}

    def fields(self, values: Mapping[str, str]) -> tuple[Field, ...]:
        """The rows to show for *values*: the spec path only for the spec focus, more only when open."""
        focus = Field(
            "focus",
            "list",
            "Focus",
            "--focus",
            "What to review for; the choices follow the category.",
            ("--focus",),
            choices=focuses(values["category"]),
            required=True,
        )
        rows = [_CATEGORY, focus]
        if values.get("focus") == _SPEC_FOCUS:
            rows.append(_SPEC)
        rows += [_SCOPE, _INSTRUCTION, _MORE]
        if values.get("more"):
            rows += [_MAX_TURNS, _TIMEOUT]
        return tuple(rows)

    def changed(self, values: Mapping[str, str]) -> dict[str, str]:
        """*values* after a change: a focus the category lacks is dropped, a lone focus is chosen."""
        options = focuses(values["category"])
        focus = values.get("focus", "")
        if focus not in options:
            focus = options[0] if len(options) == 1 else ""
        return {**values, "focus": focus}

    def refusal(self, values: Mapping[str, str]) -> str:
        """Why the command cannot be checked yet; empty when it can."""
        focus = values.get("focus", "")
        if not focus:
            return "Choose a focus"
        if not values.get("scope", "").strip():
            return "Scope is empty: name the files to review"
        if focus == _SPEC_FOCUS and not values.get("spec", "").strip():
            return "The spec focus needs a spec path"
        return ""

    def argv(self, values: Mapping[str, str]) -> tuple[str, ...]:
        """The argument words: the spec only for the spec focus, so a stale prefill never leaks."""
        words = [_CATEGORY.flag, values["category"], "--focus", values["focus"]]
        if values["focus"] == _SPEC_FOCUS:
            words += text_words(_SPEC.flag, values.get("spec", ""))
        for field in (_SCOPE, _INSTRUCTION, _MAX_TURNS, _TIMEOUT):
            words += text_words(field.flag, values.get(field.dest, ""))
        return tuple(words)
