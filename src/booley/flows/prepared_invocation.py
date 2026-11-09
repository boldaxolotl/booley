"""Typed result of endpoint preparation: what admission, invocation and reporting consume."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from booley.flows.endpoint_report_criteria import ReportScope, planned_scope
from booley.runtime.endpoint_execution import EndpointOutcome

if TYPE_CHECKING:
    from booley.flows.base import BuiltinFlow
    from booley.flows.endpoint_state import EndpointState
    from booley.targets.catalog import PreparedTargetSelection


class PreparedInvocation(ABC):
    """What preparation hands to admission, invocation and report freezing.

    An invocation owns no resources: it registers them on the endpoint's
    publication resources, which outlive the final report.
    """

    # True when ``run`` needs the admission context; otherwise it receives ``None``.
    borrows_admission: ClassVar[bool] = False

    @abstractmethod
    def report_scope(self, endpoint: EndpointState) -> ReportScope:
        """The Targets and planned Criterion keys this invocation can judge."""

    @abstractmethod
    def run(self, admission: object | None) -> EndpointOutcome:
        """Run under *admission* (``None`` when not borrowed or on a dry run)."""


@dataclass(frozen=True)
class OrdinaryRunInvocation(PreparedInvocation):
    """The endpoint's own run: Project-local endpoints, Custom Flows, Specialists, doubles."""

    run_endpoint: Callable[[], EndpointOutcome]

    def run(self, admission: object | None) -> EndpointOutcome:
        return self.run_endpoint()

    def report_scope(self, endpoint: EndpointState) -> ReportScope:
        return planned_scope(endpoint, (), endpoint.report_families(), set())


@dataclass(frozen=True)
class TargetSelectionInvocation(PreparedInvocation):
    """A built-in Flow run over the Target selection resolved during preparation."""

    flow: BuiltinFlow
    selection: PreparedTargetSelection | None
    run_endpoint: Callable[[], EndpointOutcome]

    def run(self, admission: object | None) -> EndpointOutcome:
        return self.run_endpoint()

    def report_scope(self, endpoint: EndpointState) -> ReportScope:
        args = self.flow.args
        handles = (
            self.selection.handles
            if self.selection is not None and self.selection.matches(args.work_dir, args.target)
            else ()
        )
        return planned_scope(
            endpoint,
            handles,
            self.flow.report_families(),
            self.flow.conventional_report_families(),
        )
