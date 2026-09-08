"""Transport-independent inputs for one deterministic Flow invocation.

Execution takes a copy: preparation and baseline work may change the working
inputs without changing the caller's request. Concrete Flows add typed options.
"""

from dataclasses import dataclass, field
from pathlib import Path

from booley.core.boundary import require_bool, require_int


@dataclass(kw_only=True)
class FlowRequest:
    target: str
    work_dir: Path = field(default_factory=Path.cwd)
    report_dir: Path | None = None
    diagnostic: bool = False
    dry_run: bool = False
    timeout_ms: int | None = None
    slug: str = field(default="", init=False, repr=False)
    state_file: Path | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.target, str):
            raise ValueError("target must be a Target selector string")
        self.work_dir = Path(self.work_dir)
        if self.report_dir is not None:
            self.report_dir = Path(self.report_dir)
        require_bool(vars(self), "diagnostic", field="diagnostic")
        require_bool(vars(self), "dry_run", field="dry_run")
        if self.timeout_ms is not None and require_int(self.timeout_ms, field="timeout_ms") <= 0:
            raise ValueError("timeout_ms must be positive")
