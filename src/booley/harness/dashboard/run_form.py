"""The Run wizard's form, generated from a Flow's human parser (#3580).

No Textual here. :func:`describe` reads the parser's help groups and turns each
option into a form field or routes it to the one extra-arguments field;
:func:`command_argv` turns the human's values back into the argument words the
Flow's own parser validates.

Every help group is placed once, in :data:`GROUP_PLACEMENT`: a *field* group
shows each of its options as a field and refuses an option kind the form cannot
render, an *extra* group takes options of any kind. A group the table does not
know fails :func:`describe` until someone places it, so a Flow that gains an
option never loses it silently.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from booley.flows.cli_help import option_groups
from booley.flows.cli_selection import EndpointProjectAction
from booley.harness.dashboard.launch import split_arguments

FieldKind = Literal["toggle", "three-way", "list", "text"]

# Common and each Flow's own options are fields; Output and the Expert groups are extra arguments.
GROUP_PLACEMENT: Final[dict[str, Literal["field", "extra"]]] = {
    "Common": "field",
    "Lint options": "field",
    "Simulation options": "field",
    "Synthesis options": "field",
    "FPGA options": "field",
    "Output": "extra",
    "Expert: ABC": "extra",
    "Expert: OpenROAD": "extra",
}

# Until the Target and test pickers exist, these two options say how their text is read.
_HINTS: Final[dict[str, str]] = {
    "target": "A Target name or a comma list of them; empty means automatic.",
    "test": "One Test Run; put more of them in the extra arguments.",
}
_EMPTY_TEXT: Final[dict[str, str]] = {"target": "automatic"}
TEXT_NOTE: Final = "Typed as one value: no quoting needed."
DEFAULT = "default"  # what a list or three-way field shows while nothing is chosen


class FormError(ValueError):
    """The parser has an option the form neither shows nor routes to extra arguments."""


@dataclass(frozen=True)
class Field:
    """One option (or on/off pair) the form shows as a row.

    ``flag`` is what the command spells for a toggle's or pair's "on", or the option
    that takes a list or text value; ``off_flag`` is a pair's "off".
    """

    dest: str
    kind: FieldKind
    label: str
    flag: str
    hint: str
    spellings: tuple[str, ...]  # every option string of every action: all count as placed
    off_flag: str = ""
    choices: tuple[str, ...] = ()  # a list's value spellings; a three-way's are "on" and "off"
    empty: str = ""  # what a text field shows while it has no value

    def values(self) -> tuple[str, ...]:
        """The values a choice list offers after the default, as stored."""
        return ("on", "off") if self.kind == "three-way" else self.choices

    def shown(self, value: str) -> str:
        """How the row spells *value* (``""`` is unset)."""
        if self.kind == "toggle":
            return "on" if value else "off"
        if self.kind == "text":
            return value or self.empty
        return value or DEFAULT


@dataclass(frozen=True)
class RunForm:
    """The fields of one Flow's form and the options its extra-arguments field accepts."""

    fields: tuple[Field, ...]
    extras: tuple[tuple[str, ...], ...]  # one entry per option: all its spellings

    def placed(self) -> frozenset[str]:
        """Every option spelling the form shows or routes to extra arguments."""
        spellings = {spelling for field in self.fields for spelling in field.spellings}
        return frozenset(spellings.union(*self.extras))

    def extra_hint(self) -> str:
        """The extra-arguments field's hint: how to type, and every option it takes."""
        options = ", ".join("/".join(spellings) for spellings in self.extras)
        lead = "Typed as on the command line; quote a value with spaces."
        return f"{lead} Options: {options}" if options else lead


NO_FORM: Final = RunForm((), ())


def _offered(action: argparse.Action) -> bool:
    """False for what the form never shows: help, hidden aliases and the Project selector."""
    return (
        action.dest != "help"
        and action.help != argparse.SUPPRESS
        and not isinstance(action, EndpointProjectAction)
    )


def _primary(action: argparse.Action) -> str:
    """The spelling a command writes: the first long option, else the first."""
    return next((s for s in action.option_strings if s.startswith("--")), action.option_strings[0])


def _spellings(actions: list[argparse.Action]) -> tuple[str, ...]:
    return tuple(spelling for action in actions for spelling in action.option_strings)


def _by_dest(actions: list[argparse.Action]) -> dict[str, list[argparse.Action]]:
    """The offered actions grouped by what they set, in the order the group lists them."""
    grouped: dict[str, list[argparse.Action]] = {}
    for action in actions:
        grouped.setdefault(action.dest, []).append(action)
    return grouped


def _help_text(action: argparse.Action) -> str:
    return " ".join((action.help or "").split())


def _choice_spellings(action: argparse.Action) -> tuple[str, ...]:
    """What the human types for each choice: the ``--help`` metavar's list when it has one per choice."""
    choices = tuple(action.choices or ())
    metavar = action.metavar
    if isinstance(metavar, str) and metavar.startswith("{") and metavar.endswith("}"):
        listed = tuple(metavar[1:-1].split(","))
        if len(listed) == len(choices):
            return listed
    return tuple(str(choice) for choice in choices)


def _single_field(dest: str, action: argparse.Action) -> Field | None:
    """A toggle, list or text field for the one action that sets *dest*."""
    flag, hint = _primary(action), _help_text(action)
    if action.nargs == 0 and action.const is True:
        return Field(dest, "toggle", flag, flag, hint, tuple(action.option_strings))
    if action.nargs is not None:
        return None
    if action.choices:
        return Field(
            dest,
            "list",
            flag,
            flag,
            hint,
            tuple(action.option_strings),
            choices=_choice_spellings(action),
        )
    return Field(
        dest,
        "text",
        flag,
        flag,
        f"{_HINTS.get(dest, hint)} {TEXT_NOTE}",
        tuple(action.option_strings),
        empty=_EMPTY_TEXT.get(dest, ""),
    )


def _pair_field(dest: str, actions: list[argparse.Action]) -> Field | None:
    """A three-way field for a ``--x`` / ``--no-x`` pair (store_true and store_false), else ``None``."""
    pair = {action.const: action for action in actions if action.nargs == 0}
    if len(actions) != 2 or set(pair) != {True, False}:
        return None
    on, off = _primary(pair[True]), _primary(pair[False])
    return Field(
        dest,
        "three-way",
        f"{on}/{off}",
        on,
        _help_text(pair[True]),
        _spellings(actions),
        off_flag=off,
    )


def _field(dest: str, actions: list[argparse.Action]) -> Field | None:
    """The field for the actions that set *dest*, or ``None`` when no field kind fits them."""
    if any(not action.option_strings for action in actions):
        return None  # a positional has no spelling to write
    if len(actions) == 1:
        return _single_field(dest, actions[0])
    return _pair_field(dest, actions)


def describe(parser: argparse.ArgumentParser) -> RunForm:
    """The form for *parser*; :class:`FormError` names every option it cannot place."""
    fields: list[Field] = []
    extras: list[tuple[str, ...]] = []
    problems: list[str] = []
    for title, actions in option_groups(parser):
        offered = _by_dest([action for action in actions if _offered(action)])
        if not offered:
            continue  # nothing to place: argparse's empty positionals, the help-only options
        placement = GROUP_PLACEMENT.get(title)
        if placement is None:
            names = ", ".join(_spellings([a for acts in offered.values() for a in acts]))
            problems.append(f"group {title!r} is not placed (options: {names})")
        elif placement == "extra":
            extras.extend(_spellings(acts) for acts in offered.values())
        else:
            for dest, acts in offered.items():
                field = _field(dest, acts)
                if field is None:
                    problems.append(
                        f"{'/'.join(_spellings(acts)) or dest} in group {title!r} "
                        "has a kind the form cannot show as a field"
                    )
                else:
                    fields.append(field)
    if problems:
        raise FormError("; ".join(problems))
    return RunForm(tuple(fields), tuple(extras))


def _text_words(field: Field, value: str) -> list[str]:
    """``--opt value``; a value starting with ``-`` is written ``--opt=value`` so it is never an option."""
    text = value.strip()
    if not text:
        return []
    return [f"{field.flag}={text}"] if text.startswith("-") else [field.flag, text]


def command_argv(form: RunForm, values: Mapping[str, str], extra_text: str) -> tuple[str, ...]:
    """The argument words for the field *values* (``""`` is unset) and the typed *extra_text*.

    Fields come in the parser's group order, then the extra words, of which one
    leading ``--`` is dropped as ``booley flow`` drops it, so it never lands mid-command.
    ``ArgumentsError`` when the extra text has unbalanced quotes.
    """
    words: list[str] = []
    for field in form.fields:
        value = values.get(field.dest, "")
        if field.kind == "toggle" and value:
            words.append(field.flag)
        elif field.kind == "three-way" and value:
            words.append(field.flag if value == "on" else field.off_flag)
        elif field.kind == "list" and value:
            words.extend((field.flag, value))
        elif field.kind == "text":
            words.extend(_text_words(field, value))
    extra = split_arguments(extra_text)
    return (*words, *(extra[1:] if extra[:1] == ("--",) else extra))
