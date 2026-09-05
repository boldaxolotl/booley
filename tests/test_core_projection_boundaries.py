"""Public boundary tests for isolated FuseSoC core equivalence."""

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
    comparison_root = tmp_path / "comparison"
    comparison_registry = comparison_root / core_projection.ISOLATED_REGISTRY_SUBDIR
    comparison_registry.mkdir(parents=True)
    comparison = comparison_registry / "test.core"
    comparison.write_text(
        f"CAPI=2:\n# Booley stealth core projection: marker\nroot: {comparison_root}\n",
        encoding="utf-8",
    )
    core.write_text("one line\n", encoding="utf-8")
    assert not core_projection.isolated_core_contents_equivalent(core, comparison)
    core.write_text(
        "CAPI=2:\n# Booley stealth core projection: marker\ninvalid: [\n",
        encoding="utf-8",
    )
    assert not core_projection.isolated_core_contents_equivalent(core, comparison)
    core.write_text("name: test\n# Booley stealth core projection: marker\n", encoding="utf-8")
    assert not core_projection.isolated_core_contents_equivalent(core, comparison)
    core.write_text(
        f"CAPI=2:\n# Booley stealth core projection: marker\nroot: {tmp_path}\n",
        encoding="utf-8",
    )
    assert core_projection.isolated_core_contents_equivalent(
        core, comparison, left_checkout_root=tmp_path
    )
    monkeypatch.setattr(
        Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError())
    )
    assert not core_projection.isolated_core_contents_equivalent(core, comparison)
