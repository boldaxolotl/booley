"""Every committed changelog fragment must be well formed before it reaches a release."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / ".github" / "scripts" / "release_changelog.py"
_FRAGMENTS = _ROOT / "changelog.d"


def test_committed_fragments_are_well_formed() -> None:
    """``assemble`` would reject a malformed fragment at release time; fail in the PR instead."""
    # The README keeps the directory in the repository, so the scan below is never vacuous.
    assert (_FRAGMENTS / "README.md").is_file()
    spec = importlib.util.spec_from_file_location("release_changelog", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    module.read_fragments(_FRAGMENTS)
