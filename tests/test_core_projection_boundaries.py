"""Focused boundary tests for Acceptance Basis helper modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.fusesoc import core_projection


def test_isolated_core_normalization_rejects_invalid_files_and_normalizes_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = tmp_path / core_projection.ISOLATED_REGISTRY_SUBDIR
    registry.mkdir(parents=True)
    core = registry / "test.core"
    core.write_text("one line\n", encoding="utf-8")
    assert core_projection._normalized_isolated_core(core) is None
    core.write_text(
        "CAPI=2:\n# Booley stealth core projection: marker\ninvalid: [\n",
        encoding="utf-8",
    )
    assert core_projection._normalized_isolated_core(core) is None
    core.write_text("name: test\n# Booley stealth core projection: marker\n", encoding="utf-8")
    assert core_projection._normalized_isolated_core(core) is None
    core.write_text(
        f"CAPI=2:\n# Booley stealth core projection: marker\nroot: {tmp_path}\n",
        encoding="utf-8",
    )
    marker, document = core_projection._normalized_isolated_core(core, checkout_root=tmp_path) or (
        "",
        {},
    )
    assert marker.startswith("# Booley stealth core projection:")
    assert document["root"] == "${BOOLEY_WORKTREE}"
    monkeypatch.setattr(
        Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    assert core_projection._normalized_isolated_core(core) is None
