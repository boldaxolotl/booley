from __future__ import annotations

from pathlib import Path

import pytest

from tests.architecture.import_graph import (
    Dependency,
    analyze_imports,
    file_fan_out,
    mutual_package_pairs,
    select_dependencies,
    top_level_package_sccs,
)

_FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "imports" / "booley"


def _edge(dependency: Dependency) -> tuple[str, str, str, int, int]:
    return (
        dependency.source,
        dependency.target,
        dependency.path.relative_to(_FIXTURE_ROOT).as_posix(),
        dependency.line,
        dependency.column,
    )


def test_normalizes_absolute_relative_aliased_and_package_imports() -> None:
    dependencies = analyze_imports(_FIXTURE_ROOT)

    assert tuple(map(_edge, dependencies)) == (
        ("booley", "booley.exported", "__init__.py", 1, 0),
        ("booley.absolute_allowed", "booley.domain.model", "absolute_allowed.py", 1, 0),
        (
            "booley.absolute_forbidden",
            "booley.presentation.view",
            "absolute_forbidden.py",
            1,
            0,
        ),
        (
            "booley.nested.deeper.consumer",
            "booley.nested.pkg.sibling",
            "nested/deeper/consumer.py",
            1,
            0,
        ),
        (
            "booley.nested.pkg.consumer",
            "booley.nested.pkg.sibling",
            "nested/pkg/consumer.py",
            2,
            0,
        ),
        (
            "booley.nested.pkg.consumer",
            "booley.nested.shared",
            "nested/pkg/consumer.py",
            1,
            0,
        ),
        ("booley.nested_imports", "booley.domain.model", "nested_imports.py", 4, 4),
        (
            "booley.nested_imports",
            "booley.presentation.view",
            "nested_imports.py",
            10,
            4,
        ),
    )


def test_selects_allowed_and_forbidden_prefixes() -> None:
    dependencies = analyze_imports(_FIXTURE_ROOT)

    allowed = select_dependencies(
        dependencies,
        source_prefixes=("booley.absolute_allowed",),
        target_prefixes=("booley.domain",),
    )
    forbidden = select_dependencies(
        dependencies,
        source_prefixes=("booley.absolute_forbidden",),
        target_prefixes=("booley.presentation",),
    )

    assert [dependency.target for dependency in allowed] == ["booley.domain.model"]
    assert [dependency.target for dependency in forbidden] == ["booley.presentation.view"]
    assert forbidden[0].line == 1


def test_syntax_failure_is_not_silenced(tmp_path: Path) -> None:
    package = tmp_path / "booley"
    package.mkdir()
    (package / "__init__.py").write_text("from . import broken\n", encoding="utf-8")
    (package / "broken.py").write_text("def nope(:\n", encoding="utf-8")

    with pytest.raises(SyntaxError, match="invalid syntax"):
        analyze_imports(package)


def test_read_failure_is_not_silenced(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text

    def fail_on_model(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "model.py":
            raise OSError("seeded read failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_on_model)

    with pytest.raises(OSError, match="seeded read failure"):
        analyze_imports(_FIXTURE_ROOT)


def test_every_selected_python_file_is_read(monkeypatch: pytest.MonkeyPatch) -> None:
    original = Path.read_text
    read_paths: list[Path] = []

    def record_read(path: Path, *args: object, **kwargs: object) -> str:
        read_paths.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", record_read)

    analyze_imports(_FIXTURE_ROOT)

    assert sorted(read_paths) == sorted(_FIXTURE_ROOT.rglob("*.py"))


def test_projected_package_sccs_detect_a_reverse_edge(tmp_path: Path) -> None:
    package = tmp_path / "booley"
    _write_package(package, {"alpha": ("booley.beta",), "beta": ("booley.gamma",), "gamma": ()})
    dependencies = analyze_imports(package)
    assert top_level_package_sccs(dependencies) == (
        ("booley.alpha",),
        ("booley.beta",),
        ("booley.gamma",),
    )

    (package / "gamma" / "edge.py").write_text("import booley.alpha.edge\n", encoding="utf-8")
    dependencies = analyze_imports(package)
    assert top_level_package_sccs(dependencies) == (
        ("booley.alpha", "booley.beta", "booley.gamma"),
    )
    assert mutual_package_pairs(dependencies) == ()


def test_fan_out_counts_unique_targets_deterministically(tmp_path: Path) -> None:
    package = tmp_path / "booley"
    _write_package(package, {"alpha": ("booley.beta", "booley.gamma"), "beta": (), "gamma": ()})
    source = package / "alpha" / "edge.py"
    source.write_text(
        source.read_text(encoding="utf-8") + "import booley.beta.edge\n", encoding="utf-8"
    )

    fan_out = file_fan_out(analyze_imports(package))

    assert [(item.source, item.count, item.targets) for item in fan_out] == [
        ("booley.alpha.edge", 2, ("booley.beta.edge", "booley.gamma.edge")),
    ]


def _write_package(package: Path, edges: dict[str, tuple[str, ...]]) -> None:
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for owner, targets in edges.items():
        owner_package = package / owner
        owner_package.mkdir()
        (owner_package / "__init__.py").write_text("", encoding="utf-8")
        imports = "".join(f"import {target}.edge\n" for target in targets)
        (owner_package / "edge.py").write_text(imports, encoding="utf-8")
