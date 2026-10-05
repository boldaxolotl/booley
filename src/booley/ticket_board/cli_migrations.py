"""Actionable diagnostics for rejected Ticket contract commands."""

from __future__ import annotations

import argparse
from typing import NoReturn


class TicketArgumentParser(argparse.ArgumentParser):
    """Keep unsupported commands rejected while explaining safe reauthoring."""

    def error(self, message: str) -> NoReturn:
        for command in ("contract-seal", "revise-contract"):
            invalid = f"invalid choice: {command!r}"
            command_error = any(
                isinstance(action, argparse._SubParsersAction)
                and message.startswith(f"argument {action.metavar or action.dest}:")
                for action in self._actions
            )
            if invalid in message and command_error:
                message += (
                    f"\n{command} is unsupported. Recreate an unsealed Ticket from its "
                    "authoring inputs. For a sealed Ticket requiring changed Target definitions "
                    "or source baselines, use `python -m booley.ticket_board return-to-draft "
                    "<slug>`, correct its authoring inputs, run `python -m booley.ticket_board "
                    "validate-ticket <draft-path>`, then `python -m booley.ticket_board enqueue "
                    "<slug>`. Do not edit generated metadata."
                )
                break
        super().error(message)
