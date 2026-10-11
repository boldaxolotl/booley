"""Load project-local Flows without executing their script entry points."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from booley.flows.base import BooleyFlow


class ProjectFlowLoadError(ValueError):
    """A project-local Flow cannot be loaded unambiguously."""


def load_project_flow(path: Path, name: str) -> BooleyFlow:
    """Load one named project-local Flow without executing its script entry point."""
    module = _load_module(path)
    try:
        candidates = [
            value
            for value in vars(module).values()
            if isinstance(value, type)
            and issubclass(value, BooleyFlow)
            and value is not BooleyFlow
            and value.__module__ == module.__name__
            and value.name == name
        ]
        if len(candidates) != 1:
            raise ProjectFlowLoadError(
                f"project Flow {name!r} must resolve to exactly one BooleyFlow in {path}"
            )
        return candidates[0]()
    except BaseException:
        sys.modules.pop(module.__name__, None)
        raise


def _load_module(path: Path) -> ModuleType:
    if not path.is_file():
        raise ProjectFlowLoadError(f"project Flow module does not exist: {path}")
    module_name = f"_booley_ticket_flow_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ProjectFlowLoadError(f"project Flow module cannot be loaded: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    sys.path.insert(0, str(path.resolve().parent))
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    finally:
        sys.path.pop(0)
    return module
