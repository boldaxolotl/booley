"""The protected set of a measured tree, built on a real commit export."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from booley.runtime.commit_export import export_commit
from tests.conftest import require_symlinks
from tests.runtime.test_commit_export import (
    CORE_TEMPLATE,
    commit_all,
    git,
    make_versioned_booley_directory,
    new_repository,
    write,
)

TB_CORE = (
    "CAPI=2:\n"
    "name: acme:ip:alpha:1.0\n"
    "filesets:\n"
    "  rtl:\n"
    "    files: [rtl.sv]\n"
    "  tb:\n"
    "    files: [tb.sv]\n"
    "    tags: [tb]\n"
    "targets:\n"
    "  sim:\n"
    "    flow: sim\n"
    "    flow_options: {tool: icarus}\n"
    "    filesets: [rtl, tb]\n"
    "    toplevel: top\n"
)
WAIVER_CONFIG = (
    '[coverage.waivers]\nanchor = "project_data_repository"\ndirectory = "coverage-waivers"\n'
)


def export_head(repository: Path, head: str, destination: Path) -> Path:
    return export_commit(repository, head, destination).root


def test_booley_toml_and_reviews_toml_edits_are_protected_path_edits(tmp_path: Path) -> None:
    from booley.measurement.base import changed_files, protected_edits
    from booley.measurement.protected_set import protected_set

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    write(repo / ".booley" / "booley.toml", "[project]\n")
    write(repo / ".booley" / "reviews.toml", "# reviews\n")
    fork = commit_all(repo)
    write(repo / "rtl.v", "rtl2\n")
    write(repo / ".booley" / "booley.toml", "[project]\n# edited\n")
    write(repo / ".booley" / "reviews.toml", "# edited\n")
    head = commit_all(repo, "edit")

    protected = protected_set(export_head(repo, head, tmp_path / "out"))
    edits = protected_edits(changed_files(repo, fork, head), protected)

    assert edits == (".booley/booley.toml", ".booley/reviews.toml")


def test_protected_set_lists_tb_core_glob_waiver_and_constraint_paths(tmp_path: Path) -> None:
    from booley.measurement.protected_set import protected_set

    repo = new_repository(tmp_path / "repo")
    write(
        repo / ".booley" / "booley.toml",
        '[project]\nprotected_inputs = ["scripts/*", "vectors/"]\n'
        '[coverage.waivers]\nanchor = "rtl_repository"\ndirectory = "waivers"\n',
    )
    write(repo / "alpha.core", TB_CORE)
    for name in (
        "rtl.sv",
        "tb.sv",
        "scripts/run.sh",
        "scripts/lib/x.py",
        "vectors/a.hex",
        "vectors/sub/b.hex",
        "waivers/w.toml",
        "constraints/Top.SDC",
    ):
        write(repo / name, "x\n")
    head = commit_all(repo)
    # Reach the export through a symlinked parent: entries stay relative to the real root.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "via"
    try:
        link.symlink_to(real, target_is_directory=True)
    except OSError:
        link = real
    root = export_head(repo, head, link / "out")

    protected = protected_set(root)

    assert protected == (
        ".booley/booley.toml",
        ".booley/metrics.toml",
        ".booley/pipeline.toml",
        ".booley/reviews.toml",
        ".booley/tests.toml",
        "alpha.core",
        "booley.toml",
        "constraints/Top.SDC",
        "scripts/run.sh",
        "tb.sv",
        "vectors/",
        "vectors/a.hex",
        "vectors/sub/b.hex",
        "waivers/",
    )  # no rtl.sv; scripts/* leaves scripts/lib/x.py out


def test_declaration_glob_stays_within_one_path_segment() -> None:
    from booley.config.protected_inputs import declaration_matches

    assert declaration_matches("scripts/*", "scripts/run.sh")
    assert not declaration_matches("scripts/*", "scripts/lib/x.py")
    assert declaration_matches("scripts/**", "scripts/lib/x.py")


def test_versioned_booley_directory_files_are_under_its_placement(tmp_path: Path) -> None:
    from booley.measurement.protected_set import protected_set

    checkout, booley = make_versioned_booley_directory(tmp_path, "standalone")
    write(booley / "cores" / "x.core", CORE_TEMPLATE.format(name="x"))
    git(booley, "add", "cores/x.core")
    commit_all(
        booley, staged_only=True
    )  # the Versioned Booley Directory's HEAD is what the export holds
    write(checkout / "top.core", CORE_TEMPLATE.format(name="top"))
    git(checkout, "add", "top.core")
    head = commit_all(checkout, staged_only=True)

    protected = protected_set(export_head(checkout, head, tmp_path / "out"))

    assert {".booley/booley.toml", ".booley/cores/x.core", "top.core"} <= set(protected)
    assert "rtl.v" not in protected


def _waiver_repository(tmp_path: Path, link_to: str) -> tuple[Path, str]:
    """A checkout whose waiver directory is a committed link with the given target text."""
    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    write(repo / ".booley" / "booley.toml", WAIVER_CONFIG)
    write(repo / "store" / "w.toml", "approved\n")
    (repo / ".booley" / "coverage-waivers").symlink_to(link_to)
    return repo, commit_all(repo)


@pytest.mark.skipif(sys.platform == "win32", reason="Git symlinks need core.symlinks on Windows")
def test_waiver_directory_symlinked_outside_the_checkout_fails_naming_it(tmp_path: Path) -> None:
    from booley.measurement.protected_set import UnpinnableInputError, protected_set

    require_symlinks(tmp_path)
    shared = tmp_path / "shared-waivers"
    shared.mkdir()
    repo, head = _waiver_repository(tmp_path, str(shared))
    root = export_head(repo, head, tmp_path / "out")

    with pytest.raises(UnpinnableInputError) as raised:
        protected_set(root)

    message = str(raised.value)
    assert "coverage waiver directory .booley/coverage-waivers" in message
    assert f"-> {shared}" in message
    assert "cannot be pinned to a commit" in message


@pytest.mark.skipif(sys.platform == "win32", reason="Git symlinks need core.symlinks on Windows")
def test_waiver_directory_link_inside_the_checkout_is_not_an_error(tmp_path: Path) -> None:
    from booley.measurement.protected_set import protected_set

    require_symlinks(tmp_path)
    repo, head = _waiver_repository(tmp_path, "../store")
    root = export_head(repo, head, tmp_path / "out")

    assert ".booley/coverage-waivers/" in protected_set(root)


@pytest.mark.skipif(sys.platform == "win32", reason="Git symlinks need core.symlinks on Windows")
def test_waiver_directory_link_climbing_out_through_dotdot_fails(tmp_path: Path) -> None:
    from booley.measurement.protected_set import UnpinnableInputError, protected_set

    require_symlinks(tmp_path)
    repo, head = _waiver_repository(tmp_path, "../../elsewhere")
    root = export_head(repo, head, tmp_path / "out")

    with pytest.raises(UnpinnableInputError, match=r"-> \.\./\.\./elsewhere"):
        protected_set(root)


def test_missing_waiver_directory_is_not_an_error(tmp_path: Path) -> None:
    from booley.measurement.protected_set import protected_set

    repo = new_repository(tmp_path / "repo")
    write(repo / ".booley" / "booley.toml", WAIVER_CONFIG)
    head = commit_all(repo)

    protected = protected_set(export_head(repo, head, tmp_path / "out"))

    assert ".booley/coverage-waivers/" in protected
    assert git(repo, "rev-parse", "HEAD") == head


def test_checkout_without_configuration_protects_only_the_config_file_names(
    tmp_path: Path,
) -> None:
    from booley.measurement.protected_set import protected_set

    repo = new_repository(tmp_path / "repo")
    write(repo / "rtl.v", "rtl\n")
    head = commit_all(repo)

    protected = protected_set(export_head(repo, head, tmp_path / "out"))

    assert protected == (
        ".booley/booley.toml",
        ".booley/metrics.toml",
        ".booley/pipeline.toml",
        ".booley/reviews.toml",
        ".booley/tests.toml",
        "booley.toml",
    )
