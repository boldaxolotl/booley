"""Contracts separating Booley's own source from downstream Projects."""

from __future__ import annotations

import runpy
from pathlib import Path
from unittest.mock import patch

from booley.runtime.checkout_role import (
    is_booley_source_checkout,
    source_checkout_root,
)


def _write_source_checkout(root: Path, *, marker: bool = True) -> None:
    root.mkdir()
    marker_text = "\n[tool.booley]\nsource_checkout = true\n" if marker else ""
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "booley-rtl"\n{marker_text}',
        encoding="utf-8",
    )
    (root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    (root / "src" / "booley").mkdir(parents=True)
    (root / "src" / "booley" / "__init__.py").write_text("", encoding="utf-8")
    (root / "docs" / "internals").mkdir(parents=True)
    (root / "docs" / "internals" / "CODING_PRINCIPLES.md").write_text("", encoding="utf-8")


def test_module_imports_vendored_boundary_helper(monkeypatch) -> None:
    """The standalone classifier resolves boundary.py from beside the hook."""
    module_path = (
        Path(__file__).resolve().parents[1] / "src" / "booley" / "runtime" / "checkout_role.py"
    )
    boundary_dir = module_path.parents[1] / "core"
    monkeypatch.syspath_prepend(str(boundary_dir))
    real_import = __import__

    def block_package_boundary(name, *args, **kwargs):
        if name == "booley.core.boundary":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=block_package_boundary):
        namespace = runpy.run_path(str(module_path))

    assert namespace["as_dict"]({"key": "value"}) == {"key": "value"}


def test_tracked_marker_classifies_source_without_remote_or_layout(tmp_path: Path) -> None:
    root = tmp_path / "fork"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        "[tool.booley]\nsource_checkout = true\n",
        encoding="utf-8",
    )

    assert is_booley_source_checkout(root)


def test_legacy_identity_and_layout_classify_old_branch(tmp_path: Path) -> None:
    root = tmp_path / "old-branch"
    _write_source_checkout(root, marker=False)

    assert is_booley_source_checkout(root)
    assert source_checkout_root(root / "src" / "booley") == root.resolve()


def test_distribution_name_alone_does_not_classify_downstream(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "booley-rtl"\n',
        encoding="utf-8",
    )

    assert not is_booley_source_checkout(tmp_path)


def test_source_repository_contract_has_no_project_state_directory() -> None:
    source_root = Path(__file__).resolve().parents[1]

    assert is_booley_source_checkout(source_root)
    assert not (source_root / ".booley_project").exists()
