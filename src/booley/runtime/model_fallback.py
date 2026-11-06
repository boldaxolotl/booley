"""Hidden model fallback list for ``[models]`` Specialist values (#2836, ADR 0084 amendment).

A ``[models]`` value is normally one model ID and never falls back: a provider refusal
of it raises the dedicated ``ModelUnavailableError``. A *list* of model IDs is an opt-in,
deliberately undocumented form (one Project, here the Codespaces demo, must work for
visitors with different logins). At each Specialist launch the list is walked in order:

* An entry whose Agent CLI is outside ``[agent] clis`` or has no login is skipped
  without a model call.
* The first remaining entry runs. If the provider refuses that model
  (``ModelUnavailableError``) the next entry is tried.
* Auth failures, usage limits, transient errors and timeouts propagate unchanged: the
  list never hides them.
* A refused entry is remembered for the life of this Python process (a Specialist
  launch is its own subprocess, so that covers all of its model calls), so later calls
  skip it without asking the provider again.
* With no usable entry, one ``ModelUnavailableError`` names every entry and its reason.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from booley.config.agent import AgentSettings
from booley.runtime import auth_token
from booley.runtime.agent_errors import ModelUnavailableError

logger = logging.getLogger(__name__)

T = TypeVar("T")

REASON_NOT_LOGGED_IN = "Agent CLI {cli} is not logged in"
REASON_NOT_ALLOWED = "Agent CLI {cli} is not in [agent] clis"
REASON_REMEMBERED = "refused by the provider earlier in this process"
REASON_REFUSED = "refused by the provider"

# (cli, model) -> why it was refused. Module state on purpose: the memory is the life of
# the process. Tests clear it directly; there is no test-only reset function.
_REFUSED: dict[tuple[str, str], str] = {}


@dataclass(frozen=True, slots=True)
class ModelRun:
    """The model a Specialist call ran on and the list entries passed over to reach it."""

    model: str
    cli: str
    first_choice: str
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def fell_back(self) -> bool:
        """Whether the model that ran is not the list's first entry."""
        return self.model != self.first_choice

    def note(self) -> dict[str, Any]:
        """The record stored in the Specialist result and the Review Package evidence."""
        note: dict[str, Any] = {"model": self.model, "agent_cli": self.cli}
        if self.fell_back:
            note["fallback"] = {
                "first_choice": self.first_choice,
                "skipped": [{"model": m, "reason": reason} for m, reason in self.skipped],
            }
        return note

    def summary(self) -> str:
        """One human line for a fallback: what ran and why each earlier entry did not."""
        why = "; ".join(f"{model} {reason}" for model, reason in self.skipped)
        return f"Ran on {self.model} instead of {self.first_choice} ({why})."


def entry_skip_reason(
    settings: AgentSettings, model: str, *, remembered: bool = True
) -> str | None:
    """Why list entry *model* cannot run at launch, or ``None`` when it may be tried.

    Makes no model call. Checked in the order a visitor reads it: a missing login first,
    then the allowed-CLI set, then (when *remembered*) an earlier refusal in this process.
    """
    cli = settings.cli_for_model_id(model)
    if not auth_token.cli_logged_in(cli, settings.auth_for(cli)):
        return REASON_NOT_LOGGED_IN.format(cli=cli)
    if cli not in settings.clis:
        return REASON_NOT_ALLOWED.format(cli=cli)
    if remembered and (cli, model) in _REFUSED:
        return REASON_REMEMBERED
    return None


def call_with_fallback(
    settings: AgentSettings, name: str, call: Callable[[str, str], T]
) -> tuple[T, ModelRun]:
    """Run ``call(model, cli)`` for Specialist *name* on the first usable model.

    A string pin (or none) calls once on the resolved model and lets every error
    propagate: a refusal is the #2753 error and nothing else is tried. A list walks its
    entries as the module docstring describes. Bounded by the list length.
    """
    candidates = settings.model_candidates(name)
    if candidates is None:
        model, cli = settings.model_for_specialist(name), settings.cli_for_specialist(name)
        return call(model, cli), ModelRun(model=model, cli=cli, first_choice=model)

    skipped: list[tuple[str, str]] = []
    provider = settings.cli_for_model_id(candidates[0])
    for model in candidates:
        cli = settings.cli_for_model_id(model)
        reason = entry_skip_reason(settings, model)
        if reason is not None:
            skipped.append((model, reason))
            continue
        try:
            result = call(model, cli)
        except ModelUnavailableError as refusal:
            _REFUSED[(cli, model)] = refusal.detail
            provider = refusal.provider
            skipped.append((model, REASON_REFUSED))
            logger.warning("%s: %s refused %s, trying the next [models] entry", name, cli, model)
            continue
        return result, ModelRun(
            model=model, cli=cli, first_choice=candidates[0], skipped=tuple(skipped)
        )
    raise ModelUnavailableError(
        model=candidates[0],
        provider=provider,
        settings=(f"[models] {name}",),
        detail="; ".join(f"{model}: {reason}" for model, reason in skipped),
        attempts=tuple(skipped),
    )
