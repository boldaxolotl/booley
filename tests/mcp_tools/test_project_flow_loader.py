"""Project Flow loading is unambiguous and cleans failed imports."""

import sys
from pathlib import Path

import pytest

from booley.mcp.project_flow_loader import ProjectFlowLoadError, load_project_flow


@pytest.mark.parametrize(
    "scenario", ["missing", "zero", "two", "mismatch", "execution", "constructor"]
)
def test_failed_load_removes_module_and_restores_search_path(
    tmp_path: Path, scenario: str
) -> None:
    # Unique stem: other tests in the same worker keep a loaded ``project_flow`` module.
    path = tmp_path / "loader_failure_case.py"
    source = "from booley.flows.base import BooleyFlow\n"
    if scenario in {"two", "mismatch", "constructor"}:
        source += "class First(BooleyFlow):\n    name = 'chosen'\n    def _add_args(self, parser): pass\n"
    if scenario == "two":
        source += "class Second(BooleyFlow):\n    name = 'chosen'\n    def _add_args(self, parser): pass\n"
    if scenario == "execution":
        source += "raise RuntimeError('execution failed')\n"
    if scenario == "constructor":
        source += "    def __init__(self):\n        raise RuntimeError('constructor failed')\n"
    if scenario != "missing":
        path.write_text(source)
    module_name = "_booley_ticket_flow_loader_failure_case"
    before = list(sys.path)
    with pytest.raises(
        RuntimeError if scenario in {"execution", "constructor"} else ProjectFlowLoadError
    ):
        load_project_flow(path, "other" if scenario == "mismatch" else "chosen")
    assert module_name not in sys.modules
    assert sys.path == before


def test_successful_load_keeps_module_without_running_script_entry(tmp_path: Path) -> None:
    path = tmp_path / "working.py"
    path.write_text(
        "from booley.flows.base import BooleyFlow\n"
        "class Working(BooleyFlow):\n    name = 'working'\n    def _add_args(self, parser): pass\n"
        "if __name__ == '__main__':\n    raise AssertionError('script entry ran')\n"
    )
    try:
        flow = load_project_flow(path, "working")
        assert flow.name == "working"
        assert type(flow).__module__ in sys.modules
    finally:
        sys.modules.pop("_booley_ticket_flow_working", None)
