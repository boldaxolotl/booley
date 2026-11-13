"""Deterministic release changelog checker contracts."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / ".github" / "scripts" / "release_changelog.py"


def _load():
    spec = importlib.util.spec_from_file_location("release_changelog", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _text() -> str:
    return (
        "# Changelog\n\n## Unreleased\n\n## 1.2.0 - 31 AUG 2026\n\n### Bug fixes\n\n- Fixed it.\n"
    )


def test_sync_and_validate_require_byte_identical_mirror(tmp_path: Path) -> None:
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    root.write_text(_text(), encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    packaged.write_text("drift\n", encoding="utf-8")

    with pytest.raises(checker.ChangelogError, match="differs"):
        checker.validate(root, packaged)

    checker.synchronize(root, packaged)
    checker.validate(root, packaged, target="1.2.0")
    assert root.read_bytes() == packaged.read_bytes()


def test_target_and_supplied_notes_must_match_exactly(tmp_path: Path) -> None:
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    notes = tmp_path / "notes.md"
    root.write_text(_text(), encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    packaged.write_text(_text(), encoding="utf-8")
    notes.write_text("different\n", encoding="utf-8")

    with pytest.raises(checker.ChangelogError, match="differ"):
        checker.validate(root, packaged, target="1.2.0", notes_file=notes)

    notes.write_text(checker.release_entry(_text(), "1.2.0").body, encoding="utf-8")
    checker.validate(root, packaged, target="1.2.0", notes_file=notes)


def test_extract_preserves_crlf_body_bytes(tmp_path: Path, monkeypatch) -> None:
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    output = tmp_path / "notes.md"
    raw = _text().replace("\n", "\r\n").encode()
    root.write_bytes(raw)
    (tmp_path / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    packaged.write_bytes(raw)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "--root",
            str(root),
            "--packaged",
            str(packaged),
            "extract",
            "--target",
            "1.2.0",
            "--output",
            str(output),
        ],
    )

    assert checker.main() == 0
    assert output.read_bytes() == b"\r\n### Bug fixes\r\n\r\n- Fixed it.\r\n"


def test_validate_rejects_dated_heading_above_version(tmp_path: Path) -> None:
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    root.write_text(_text(), encoding="utf-8")
    packaged.write_text(_text(), encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.1.0\n", encoding="utf-8")

    with pytest.raises(checker.ChangelogError, match=r"1\.2\.0 - 31 AUG 2026.*VERSION 1\.1\.0"):
        checker.validate(root, packaged)


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(_text().replace("## Unreleased\n\n", ""), id="removed"),
        pytest.param(
            _text()
            .replace("## Unreleased\n\n", "")
            .replace("- Fixed it.\n", "- Fixed it.\n\n## Unreleased\n"),
            id="below-release",
        ),
    ],
)
def test_validate_requires_unreleased_as_first_section(tmp_path: Path, text: str) -> None:
    """Release 0.4.0 dropped the empty heading, which broke a changelog test on main."""
    checker = _load()
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "packaged.md"
    root.write_text(text, encoding="utf-8")
    packaged.write_text(text, encoding="utf-8")
    (tmp_path / "VERSION").write_text("1.2.0\n", encoding="utf-8")

    with pytest.raises(
        checker.ChangelogError, match=r"first changelog section must be '## Unreleased'"
    ):
        checker.validate(root, packaged)


def test_real_changelogs_validate_against_version() -> None:
    _load().validate()


# ---------------------------------------------------------------------------
# Changelog fragments: ``assemble`` and the ``validate --target`` guard.
# ---------------------------------------------------------------------------

_RELEASE = "## 1.2.0 - 31 AUG 2026\n\n### Bug fixes\n\n- Fixed it.\n"


def _layout(tmp_path: Path, unreleased: str = "") -> tuple[Path, Path, Path]:
    """Write a changelog, its identical mirror, VERSION and an empty fragment directory."""
    text = f"# Changelog\n\n## Unreleased\n\n{unreleased}{_RELEASE}"
    root = tmp_path / "CHANGELOG.md"
    packaged = tmp_path / "mirror" / "CHANGELOG.md"
    packaged.parent.mkdir()
    root.write_bytes(text.encode())
    packaged.write_bytes(text.encode())
    (tmp_path / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    fragments = tmp_path / "changelog.d"
    fragments.mkdir()
    (fragments / "README.md").write_text("# Fragments\n", encoding="utf-8")
    return root, packaged, fragments


def _snapshot(*roots: Path) -> dict[str, tuple[bytes | None, int]]:
    """Bytes (None for directories) and mtime of every file below each root."""
    seen: dict[str, tuple[bytes | None, int]] = {}
    for root in roots:
        for path in [root, *sorted(root.rglob("*"))] if root.is_dir() else [root]:
            data = path.read_bytes() if path.is_file() else None
            # Directory mtimes move when a temporary file comes and goes; files must not.
            seen[str(path)] = (data, path.stat().st_mtime_ns if path.is_file() else 0)
    return seen


def _main(monkeypatch: pytest.MonkeyPatch, root: Path, packaged: Path, *args: str) -> int:
    monkeypatch.setattr(
        sys, "argv", [str(SCRIPT), "--root", str(root), "--packaged", str(packaged), *args]
    )
    return _load().main()


def test_assemble_without_fragments_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    packaged.write_bytes(b"stale mirror\n")  # a write would repair it

    # Only the README (and a hidden editor file) is present.
    (fragments / ".DS_Store").write_bytes(b"\x00")
    before = _snapshot(tmp_path)
    assert checker.assemble(root, packaged) == []
    assert _main(monkeypatch, root, packaged, "assemble") == 0
    assert "no changelog fragments" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before

    # A missing fragment directory is the same.
    (fragments / ".DS_Store").unlink()
    (fragments / "README.md").unlink()
    fragments.rmdir()
    before = _snapshot(tmp_path)
    assert checker.assemble(root, packaged) == []
    assert _snapshot(tmp_path) == before


def test_assemble_appends_fragments_in_order_and_mirrors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / "12.md").write_text("- Twelve.\n  More twelve.\n\n  Twelve again.\n", "utf-8")
    (fragments / "3.md").write_text("\n- Three.\n\n\n", "utf-8")
    (fragments / "beta.md").write_text("- Beta.\n", "utf-8")
    (fragments / "alpha.md").write_text("- Alpha.\n", "utf-8")
    readme = (fragments / "README.md").read_bytes()

    assert _main(monkeypatch, root, packaged, "assemble") == 0

    assembled = capsys.readouterr().out.splitlines()
    assert [Path(line.removeprefix("assembled ")).name for line in assembled] == [
        "3.md",
        "12.md",
        "alpha.md",
        "beta.md",
    ]
    expected = (
        "# Changelog\n\n## Unreleased\n\n- Three.\n- Twelve.\n  More twelve.\n\n  Twelve again.\n"
        f"- Alpha.\n- Beta.\n\n{_RELEASE}"
    )
    assert root.read_bytes() == expected.encode()
    assert packaged.read_bytes() == root.read_bytes()
    assert [path.name for path in fragments.iterdir()] == ["README.md"]
    assert (fragments / "README.md").read_bytes() == readme

    # A second run changes nothing.
    before = _snapshot(tmp_path)
    assert checker.assemble(root, packaged) == []
    assert _snapshot(tmp_path) == before
    checker.validate(root, packaged, target="1.2.0")


def test_assemble_keeps_existing_unreleased_text(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path, unreleased="- Old one.\n- Old two.\n\n")
    (fragments / "9.md").write_text("- New.\n", encoding="utf-8")

    checker.assemble(root, packaged)

    expected = f"# Changelog\n\n## Unreleased\n\n- Old one.\n- Old two.\n- New.\n\n{_RELEASE}"
    assert root.read_bytes() == expected.encode()
    assert packaged.read_bytes() == root.read_bytes()

    # Unreleased as the last section: the file still ends with one newline.
    eof = "# Changelog\n\n## Unreleased\n\n- Old.\n"
    root.write_bytes(eof.encode())
    (fragments / "10.md").write_text("- Newer.\n", encoding="utf-8")
    checker.assemble(root, packaged)
    assert root.read_bytes() == f"{eof}- Newer.\n".encode()


def test_assemble_preserves_crlf_changelog(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path, unreleased="- Old.\n\n")
    root.write_bytes(root.read_bytes().replace(b"\n", b"\r\n"))
    (fragments / "9.md").write_bytes(b"- New.\n  Cont.\n")

    checker.assemble(root, packaged)

    expected = f"# Changelog\n\n## Unreleased\n\n- Old.\n- New.\n  Cont.\n\n{_RELEASE}"
    assert root.read_bytes() == expected.replace("\n", "\r\n").encode()
    assert packaged.read_bytes() == root.read_bytes()


def test_assemble_accepts_bullet_markers_and_lazy_continuation(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / "1.md").write_text("* Star.\n+ Plus.\n- Dash\nlazily continued.\n", "utf-8")

    checker.assemble(root, packaged)

    expected = f"# Changelog\n\n## Unreleased\n\n* Star.\n+ Plus.\n- Dash\nlazily continued.\n\n{_RELEASE}"
    assert root.read_bytes() == expected.encode()


# (file name, content or None for a directory, text the error must contain)
_MALFORMED = [
    pytest.param("7.md", b"", "empty", id="empty"),
    pytest.param("7.md", b"\n  \n\n", "empty", id="blank"),
    pytest.param("7.md", b"- ok \xff\n", "UTF-8", id="not-utf8"),
    pytest.param("7.md", b"- a\n\n## Heading\n", "heading", id="heading"),
    pytest.param("7.md", b"# Title\n- a\n", "heading", id="h1-first"),
    pytest.param("7.md", b"- a\n  ### x\n", "heading", id="indented-heading"),
    pytest.param("7.md", b"- ## x\n", "heading", id="heading-after-bullet"),
    pytest.param("7.md", b"Just prose.\n", "bullet", id="prose"),
    pytest.param("7.md", b"- a\n\nprose\n", "bullet", id="prose-after-blank"),
    pytest.param("7.md", b"- a\n===\n", "underline", id="setext"),
    pytest.param("Foo.md", b"- a\n", "name", id="uppercase-name"),
    pytest.param("3606.txt", b"- a\n", "name", id="wrong-extension"),
    pytest.param("sub", None, "regular file", id="directory"),
]


@pytest.mark.parametrize(("name", "content", "reason"), _MALFORMED)
def test_assemble_rejects_malformed_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: bytes | None,
    reason: str,
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / "1.md").write_text("- Valid sibling.\n", encoding="utf-8")
    if content is None:
        (fragments / name).mkdir()
        (fragments / name / "inner.md").write_bytes(b"- x\n")
    else:
        (fragments / name).write_bytes(content)
    before = _snapshot(tmp_path)

    assert _main(monkeypatch, root, packaged, "assemble") == 2

    error = capsys.readouterr().err
    assert error.startswith("error:")
    assert name in error
    assert reason in error
    assert "1.md" not in error  # the valid sibling is not blamed
    assert _snapshot(tmp_path) == before
    with pytest.raises(checker.ChangelogError, match=reason):
        checker.assemble(root, packaged)
    assert _snapshot(tmp_path) == before


def test_assemble_names_every_malformed_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, packaged, fragments = _layout(tmp_path)
    (fragments / "5.md").write_bytes(b"")
    (fragments / "6.md").write_bytes(b"prose\n")

    assert _main(monkeypatch, root, packaged, "assemble") == 2

    error = capsys.readouterr().err
    assert "5.md" in error
    assert "6.md" in error


def test_assemble_requires_unreleased_section_before_touching_anything(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    root.write_bytes(_RELEASE.encode())
    (fragments / "1.md").write_text("- a\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    with pytest.raises(checker.ChangelogError, match="first changelog section"):
        checker.assemble(root, packaged)

    assert _snapshot(tmp_path) == before


def test_assemble_reports_a_partial_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the changelog changed tells the operator to restore both files."""
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / "1.md").write_text("- a\n", encoding="utf-8")
    real_replace = checker.os.replace
    calls: list[Path] = []

    def failing_replace(source: Path, destination: Path) -> None:
        calls.append(Path(destination))
        if len(calls) == 2:  # the mirror, after the changelog was already replaced
            raise PermissionError("denied")
        real_replace(source, destination)

    monkeypatch.setattr(checker.os, "replace", failing_replace)

    with pytest.raises(checker.ChangelogError, match=r"already updated.*restore"):
        checker.assemble(root, packaged)

    assert (fragments / "1.md").exists()  # fragments are only removed last


def test_assemble_leaves_nothing_behind_when_the_first_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / "1.md").write_text("- a\n", encoding="utf-8")
    before = _snapshot(tmp_path)

    def failing_replace(source: Path, destination: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(checker.os, "replace", failing_replace)

    with pytest.raises(PermissionError):
        checker.assemble(root, packaged)

    assert not list(tmp_path.rglob("*.tmp"))
    assert _snapshot(tmp_path) == before


def test_validate_target_fails_while_fragments_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / ".DS_Store").write_bytes(b"\x00")  # hidden files are not fragments
    checker.validate(root, packaged, target="1.2.0")

    (fragments / "3606.md").write_text("- a\n", encoding="utf-8")
    (fragments / "fix-typo.md").write_text("- b\n", encoding="utf-8")

    with pytest.raises(checker.ChangelogError, match=r"3606\.md.*fix-typo\.md"):
        checker.validate(root, packaged, target="1.2.0")
    assert _main(monkeypatch, root, packaged, "validate", "--target", "1.2.0") == 2
    error = capsys.readouterr().err
    assert "3606.md" in error
    assert "fix-typo.md" in error
    assert "assemble" in error

    # Without a target, and for ``extract`` of a published release, fragments are fine.
    checker.validate(root, packaged)
    assert _main(monkeypatch, root, packaged, "validate") == 0
    assert _main(monkeypatch, root, packaged, "extract", "--target", "1.2.0") == 0
    assert capsys.readouterr().out.endswith("- Fixed it.\n")

    checker.assemble(root, packaged)
    checker.validate(root, packaged, target="1.2.0")
