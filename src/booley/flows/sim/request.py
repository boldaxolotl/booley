"""Structured input for the sim Flow."""

from dataclasses import dataclass
from pathlib import Path

from booley.flows.request import FlowRequest

from .mode import SimulationMode


@dataclass(kw_only=True)
class SimRequest(FlowRequest):
    # Simulation alone permits a manifest-only resume at the generic transport
    # boundary.  Validation below immediately restores the Target XOR resume
    # invariant before any endpoint side effect.
    target: str = ""
    # ``None`` is meaningful transport presence.  Campaign preparation chooses
    # SIMULATE only on the new-campaign path; resume must be able to distinguish
    # an omitted mode from an explicitly supplied one.
    mode: SimulationMode | None = None
    # ``None`` preserves omission across typed and transport boundaries.  An
    # empty tuple is not the same request: callers that explicitly provide a
    # selector must name at least one test.
    test: tuple[str, ...] | None = None
    tests_file: Path | None = None
    resume_from: Path | None = None
    trace: bool = False
    coverage: bool = False
    result_verbosity: str = "compact"
    no_kill: bool = False
    _legacy_standalone_without_elab: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.coverage, bool):
            raise ValueError("coverage must be boolean")
        self._normalize_campaign_paths()
        self._normalize_test_selection()
        self._validate_resume_shape()
        self._consume_tests_file()
        if self.result_verbosity not in ("compact", "full"):
            raise ValueError("result_verbosity must be compact or full")

    def _normalize_campaign_paths(self) -> None:
        if self.mode is not None:
            self.mode = SimulationMode(self.mode)
        if self.tests_file is not None:
            self.tests_file = Path(self.tests_file)
        if self.resume_from is not None:
            self.resume_from = Path(self.resume_from)

    def _normalize_test_selection(self) -> None:
        if self.test is not None:
            if isinstance(self.test, list):
                self.test = tuple(self.test)
            if not isinstance(self.test, tuple) or not self.test or any(
                not isinstance(name, str) or not name for name in self.test
            ):
                raise ValueError("test must be a non-empty array of exact names")
            if len(set(self.test)) != len(self.test):
                raise ValueError("test names must be unique")
        if self.test is not None and self.tests_file is not None:
            raise ValueError("test and tests_file are mutually exclusive")

    def _consume_tests_file(self) -> None:
        """Give typed callers the same exact-selection semantics as the CLI."""
        if self.tests_file is None:
            return
        try:
            text = self.tests_file.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read tests_file {self.tests_file}: {exc}") from exc
        names = tuple(
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        )
        if not names:
            raise ValueError("tests_file contains no test names")
        if len(set(names)) != len(names):
            raise ValueError("test names must be unique")
        self.test = names
        self.tests_file = None

    def _validate_resume_shape(self) -> None:
        if self.resume_from is not None:
            conflicts = [
                name
                for present, name in (
                    (bool(self.target), "target"),
                    (self.test is not None, "test"),
                    (self.tests_file is not None, "tests_file"),
                    (self.mode is not None, "mode"),
                    (self.coverage, "coverage"),
                    (self.trace, "trace"),
                )
                if present
            ]
            if conflicts:
                raise ValueError(
                    "resume_from cannot be combined with " + ", ".join(conflicts)
                )
        if self.mode is not None and self.mode.elaborates_only and self.resume_from is not None:
            raise ValueError("elaboration modes cannot resume a Simulation Campaign")
