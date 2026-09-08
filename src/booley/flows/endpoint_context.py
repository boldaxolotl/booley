"""Compatibility facade for Project-local endpoints and Specialists.

Only legacy extensions inherit CLI parsing and the historical hook surface.
Built-in Flows compose a FlowSession instead.
"""

from __future__ import annotations

import argparse
from abc import abstractmethod

from booley.flows import endpoint_cli, endpoint_session
from booley.flows.endpoint_session import PreparedExecution
from booley.flows.endpoint_state import EndpointState
from booley.runtime.endpoint_execution import EndpointOutcome, ExecutionResult


class EndpointContext(EndpointState):
    def __init__(self) -> None:
        super().__init__()
        self._cli_parser: argparse.ArgumentParser | None = None
        # Supported legacy hooks may initialize state used by subclass constructors.
        self._cli_parser = self._parser

    def _add_common_args(self) -> None:
        endpoint_cli.add_common_args(
            self._parser,
            target_required=self.target_required,
            accepts_target=self.accepts_target,
            target_help=self.target_help,
        )

    @abstractmethod
    def _add_args(self, parser: argparse.ArgumentParser) -> None:
        """Add endpoint-specific arguments."""

    def parse_args(self, argv: list[str] | None = None) -> argparse.Namespace:
        return endpoint_cli.parse_args(self, argv)

    def steering_text(self) -> str:
        """Return steering text from repeated ``--steer`` values."""
        raw = getattr(self.args, "steer", None)
        if raw is None:
            return ""
        values = raw if isinstance(raw, list) else [raw]
        values = [str(v) for v in values]
        return "\n".join(v for v in values if v)

    def _prepare_cli_execution(
        self,
        argv: list[str] | None,
    ) -> PreparedExecution | EndpointOutcome:
        self.parse_args(argv)
        return endpoint_session.prepare_execution(self)

    def execute_cli(self, argv: list[str] | None = None) -> ExecutionResult:
        return endpoint_cli.execute_cli(self, argv)

    def main(self, argv: list[str] | None = None) -> int:
        return endpoint_cli.main(self, argv)

    def cli(self) -> None:
        return endpoint_cli.cli(self)

    @property
    def _parser(self) -> argparse.ArgumentParser:
        if self._cli_parser is None:
            self._cli_parser = argparse.ArgumentParser(
                prog=self.name or self.__class__.__name__, description=self.description
            )
            self._add_common_args()
            self._add_args(self._cli_parser)
        return self._cli_parser
