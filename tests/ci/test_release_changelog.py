"""Deterministic release changelog checker contracts."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from booley.runtime.timefmt import format_human_date

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
# Changelog fragments: ``cut`` and the ``validate --target`` guard.
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


def _write(fragments: Path, name: str, text: str) -> None:
    path = fragments / name
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(text.encode())


def test_cut_without_selected_fragments_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    packaged.write_bytes(b"stale mirror\n")  # a write would repair it
    (fragments / ".DS_Store").write_bytes(b"\x00")
    _write(fragments, "next-minor/5.major.md", "- Held.\n")
    (fragments / "next-minor" / ".keep").write_bytes(b"")
    before = _snapshot(tmp_path)

    # A patch target skips the held fragment, so nothing is selected.
    assert checker.cut(root, packaged, target="1.2.1") == []
    assert _main(monkeypatch, root, packaged, "cut", "--target", "1.2.1") == 3
    assert "no changelog fragments selected for 1.2.1" in capsys.readouterr().out
    assert _snapshot(tmp_path) == before

    # A missing fragment directory is the same.
    (fragments / "next-minor" / ".keep").unlink()
    (fragments / "next-minor" / "5.major.md").unlink()
    (fragments / "next-minor").rmdir()
    (fragments / ".DS_Store").unlink()
    (fragments / "README.md").unlink()
    fragments.rmdir()
    before = _snapshot(tmp_path)
    assert checker.cut(root, packaged, target="1.3.0") == []
    assert _snapshot(tmp_path) == before


def test_cut_writes_minor_entry_by_section_and_mirrors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "next-minor/5.major.md", "- Five.\n")
    _write(fragments, "12.feature.md", "- Twelve.\n  More twelve.\n\n  Twelve again.\n")
    _write(fragments, "3.feature.md", "\n- Three.\n\n\n")
    _write(fragments, "alpha.qol.md", "- Alpha.\n")
    _write(fragments, "7.qol.md", "- Seven.\n")
    _write(fragments, "3.fix.md", "- Fix three.\n")
    _write(fragments, "next-minor/2.fix.md", "- Fix two.\n")
    _write(fragments, "beta.internal.md", "- Beta.\n")
    _write(fragments, "next-minor/3.upgrade.md", "- Upgrade three.\n")
    (fragments / "next-minor" / ".keep").write_bytes(b"")
    readme = (fragments / "README.md").read_bytes()

    assert (
        _main(monkeypatch, root, packaged, "cut", "--target", "1.3.0", "--date", "2026-12-01") == 0
    )

    used = capsys.readouterr().out.splitlines()
    assert sorted(Path(line.removeprefix("cut ")).name for line in used) == sorted(
        [
            "5.major.md",
            "12.feature.md",
            "3.feature.md",
            "alpha.qol.md",
            "7.qol.md",
            "3.fix.md",
            "2.fix.md",
            "beta.internal.md",
            "3.upgrade.md",
        ]
    )
    expected = (
        "# Changelog\n\n## Unreleased\n\n## 1.3.0 - 01 DEC 2026\n\n"
        "### Major features\n\n- Five.\n\n"
        "### Regular features\n\n- Three.\n- Twelve.\n  More twelve.\n\n  Twelve again.\n\n"
        "### Quality of life\n\n- Seven.\n- Alpha.\n\n"
        "### Bug fixes\n\n- Fix two.\n- Fix three.\n\n"
        "### Internal work\n\n- Beta.\n\n"
        "### Upgrade notes\n\n- Upgrade three.\n\n"
        f"{_RELEASE}"
    )
    assert root.read_bytes() == expected.encode()
    assert packaged.read_bytes() == root.read_bytes()
    assert sorted(path.name for path in fragments.iterdir()) == ["README.md", "next-minor"]
    assert [path.name for path in (fragments / "next-minor").iterdir()] == [".keep"]
    assert (fragments / "README.md").read_bytes() == readme

    # A repeated cut before the release bumps VERSION would add a second 1.3.0 entry.
    _write(fragments, "9.fix.md", "- Nine.\n")
    before = _snapshot(tmp_path)
    with pytest.raises(checker.ChangelogError, match="duplicate"):
        checker.cut(root, packaged, target="1.3.0")
    assert _snapshot(tmp_path) == before

    (fragments / "9.fix.md").unlink()
    (tmp_path / "VERSION").write_text("1.3.0\n", encoding="utf-8")
    checker.validate(root, packaged, target="1.3.0")


def test_cut_patch_target_keeps_held_fragments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "4.fix.md", "- Fix four.\n")
    _write(fragments, "next-minor/5.major.md", "- Five.\n")
    _write(fragments, "next-minor/6.upgrade.md", "- Six.\n")
    _write(fragments, "next-minor/7.fix.md", "- Fix seven.\n")
    held = _snapshot(fragments / "next-minor")

    before_call = datetime.now(UTC).date()
    assert _main(monkeypatch, root, packaged, "cut", "--target", "1.2.1") == 0
    after_call = datetime.now(UTC).date()

    candidates = {
        f"# Changelog\n\n## Unreleased\n\n## 1.2.1 - {format_human_date(day)}\n\n"
        f"### Bug fixes\n\n- Fix four.\n\n{_RELEASE}"
        for day in (before_call, after_call)
    }
    assert root.read_bytes().decode() in candidates
    assert b"Major features" not in root.read_bytes()
    assert b"Upgrade notes" not in root.read_bytes()
    assert packaged.read_bytes() == root.read_bytes()
    assert not (fragments / "4.fix.md").exists()
    assert _snapshot(fragments / "next-minor") == held

    (tmp_path / "VERSION").write_text("1.2.1\n", encoding="utf-8")
    checker.validate(root, packaged, target="1.2.1")


def test_cut_uses_a_given_date_object(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "1.fix.md", "- One.\n")

    checker.cut(root, packaged, target="1.2.1", released=date(2027, 1, 31))

    assert b"## 1.2.1 - 31 JAN 2027\n" in root.read_bytes()


def test_cut_preserves_crlf_changelog(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    root.write_bytes(b"# Changelog\r\n\r\n## Unreleased\r\n")  # Unreleased is the last section
    _write(fragments, "9.fix.md", "- New.\n  Cont.\n")

    checker.cut(root, packaged, target="1.3.0", released=date(2026, 12, 1))

    expected = "# Changelog\n\n## Unreleased\n\n## 1.3.0 - 01 DEC 2026\n\n### Bug fixes\n\n- New.\n  Cont.\n"
    assert root.read_bytes() == expected.replace("\n", "\r\n").encode()
    assert packaged.read_bytes() == root.read_bytes()


def test_cut_accepts_bullet_markers_and_lazy_continuation(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "1.fix.md", "* Star.\n+ Plus.\n- Dash\nlazily continued.\n")

    checker.cut(root, packaged, target="1.3.0", released=date(2026, 12, 1))

    assert (
        root.read_bytes()
        .decode()
        .startswith(
            "# Changelog\n\n## Unreleased\n\n## 1.3.0 - 01 DEC 2026\n\n### Bug fixes\n\n"
            "* Star.\n+ Plus.\n- Dash\nlazily continued.\n\n## 1.2.0"
        )
    )


# (file name, content or None for a directory, text the error must contain)
_MALFORMED = [
    pytest.param("7.fix.md", b"", "empty", id="empty"),
    pytest.param("7.fix.md", b"\n  \n\n", "empty", id="blank"),
    pytest.param("7.fix.md", b"- ok \xff\n", "UTF-8", id="not-utf8"),
    pytest.param("7.fix.md", b"- a\n\n## Heading\n", "heading", id="heading"),
    pytest.param("7.fix.md", b"# Title\n- a\n", "heading", id="h1-first"),
    pytest.param("7.fix.md", b"- a\n  ### x\n", "heading", id="indented-heading"),
    pytest.param("7.fix.md", b"- ## x\n", "heading", id="heading-after-bullet"),
    pytest.param("7.fix.md", b"Just prose.\n", "bullet", id="prose"),
    pytest.param("7.fix.md", b"- a\n\nprose\n", "bullet", id="prose-after-blank"),
    pytest.param("7.fix.md", b"- a\n===\n", "underline", id="setext"),
    pytest.param("7.md", b"- a\n", "missing kind", id="missing-kind"),
    pytest.param("7.bogus.md", b"- a\n", "unknown kind", id="unknown-kind"),
    pytest.param("7.major.md", b"- a\n", "next-minor", id="major-not-held"),
    pytest.param("7.upgrade.md", b"- a\n", "next-minor", id="upgrade-not-held"),
    pytest.param("Foo.fix.md", b"- a\n", "name", id="uppercase-name"),
    pytest.param("3606.fix.txt", b"- a\n", "name", id="wrong-extension"),
    pytest.param("next-minor/7.fix.md", b"- a\n\n## Heading\n", "heading", id="held-unselected"),
    pytest.param("next-minor/7.md", b"- a\n", "missing kind", id="held-missing-kind"),
    pytest.param("next-minor", b"- a\n", "invalid name", id="held-directory-is-file"),
    pytest.param("sub", None, "regular file", id="directory"),
    pytest.param("next-minor/sub", None, "regular file", id="held-directory"),
]


@pytest.mark.parametrize(("name", "content", "reason"), _MALFORMED)
def test_cut_rejects_malformed_fragment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    content: bytes | None,
    reason: str,
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "1.fix.md", "- Valid sibling.\n")
    target = fragments / name
    target.parent.mkdir(exist_ok=True)
    if content is None:
        target.mkdir()
        (target / "inner.md").write_bytes(b"- x\n")
    else:
        target.write_bytes(content)
    before = _snapshot(tmp_path)

    assert _main(monkeypatch, root, packaged, "cut", "--target", "1.2.1") == 2

    error = capsys.readouterr().err
    assert error.startswith("error:")
    assert target.name in error
    assert reason in error
    assert "1.fix.md" not in error  # the valid sibling is not blamed
    assert _snapshot(tmp_path) == before
    with pytest.raises(checker.ChangelogError, match=reason):
        checker.cut(root, packaged, target="1.2.1")
    with pytest.raises(checker.ChangelogError, match=reason):
        checker.read_fragments(fragments)  # the fragment format test's entry point
    assert _snapshot(tmp_path) == before


def test_cut_names_every_malformed_fragment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "5.fix.md", "")
    _write(fragments, "next-minor/6.major.md", "prose\n")

    assert _main(monkeypatch, root, packaged, "cut", "--target", "1.3.0") == 2

    error = capsys.readouterr().err
    assert "5.fix.md" in error
    assert "6.major.md" in error


@pytest.mark.parametrize(
    ("target", "unreleased", "reason"),
    [
        pytest.param("1.2.0", "", "not greater than VERSION", id="not-above-version"),
        pytest.param("1.3", "", "unsupported version", id="not-a-version"),
        pytest.param("1.3.0", "- Pending.\n\n", "Unreleased", id="unreleased-content"),
    ],
)
def test_cut_rejects_invalid_target_or_unreleased_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    target: str,
    unreleased: str,
    reason: str,
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path, unreleased=unreleased)
    _write(fragments, "1.fix.md", "- Valid.\n")
    before = _snapshot(tmp_path)

    assert _main(monkeypatch, root, packaged, "cut", "--target", target) == 2

    assert reason in capsys.readouterr().err
    assert _snapshot(tmp_path) == before
    with pytest.raises(checker.ChangelogError, match=reason):
        checker.cut(root, packaged, target=target)
    assert _snapshot(tmp_path) == before


def test_cut_requires_unreleased_section_before_touching_anything(tmp_path: Path) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    root.write_bytes(_RELEASE.encode())
    _write(fragments, "1.fix.md", "- a\n")
    before = _snapshot(tmp_path)

    with pytest.raises(checker.ChangelogError, match="first changelog section"):
        checker.cut(root, packaged, target="1.3.0")

    assert _snapshot(tmp_path) == before


def test_cut_reports_a_partial_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failure after the changelog changed tells the operator to restore both files."""
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "1.fix.md", "- a\n")
    real_replace = checker.os.replace
    calls: list[Path] = []

    def failing_replace(source: Path, destination: Path) -> None:
        calls.append(Path(destination))
        if len(calls) == 2:  # the mirror, after the changelog was already replaced
            raise PermissionError("denied")
        real_replace(source, destination)

    monkeypatch.setattr(checker.os, "replace", failing_replace)

    with pytest.raises(checker.ChangelogError, match=r"already updated.*restore"):
        checker.cut(root, packaged, target="1.3.0")

    assert (fragments / "1.fix.md").exists()  # fragments are only removed last


def test_cut_leaves_nothing_behind_when_the_first_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    _write(fragments, "1.fix.md", "- a\n")
    before = _snapshot(tmp_path)

    def failing_replace(source: Path, destination: Path) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(checker.os, "replace", failing_replace)

    with pytest.raises(PermissionError):
        checker.cut(root, packaged, target="1.3.0")

    assert not list(tmp_path.rglob("*.tmp"))
    assert _snapshot(tmp_path) == before


def test_validate_target_fails_while_fragments_remain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    checker = _load()
    root, packaged, fragments = _layout(tmp_path)
    (fragments / ".DS_Store").write_bytes(b"\x00")  # hidden files are not fragments
    checker.validate(root, packaged, target="1.2.0")

    _write(fragments, "3606.fix.md", "- a\n")
    _write(fragments, "fix-typo.fix.md", "- b\n")
    _write(fragments, "next-minor/5.major.md", "- c\n")

    with pytest.raises(checker.ChangelogError, match=r"3606\.fix\.md.*fix-typo\.fix\.md"):
        checker.validate(root, packaged, target="1.2.0")
    assert _main(monkeypatch, root, packaged, "validate", "--target", "1.2.0") == 2
    error = capsys.readouterr().err
    assert "3606.fix.md" in error
    assert "fix-typo.fix.md" in error
    assert "next-minor/5.major.md" in error
    assert "cut --target 1.2.0" in error

    # Without a target, and for ``extract`` of a published release, fragments are fine.
    checker.validate(root, packaged)
    assert _main(monkeypatch, root, packaged, "validate") == 0
    assert _main(monkeypatch, root, packaged, "extract", "--target", "1.2.0") == 0
    assert capsys.readouterr().out.endswith("- Fixed it.\n")

    # After a patch cut and the VERSION bump, held fragments may remain.
    (fragments / "3606.fix.md").unlink()
    (fragments / "fix-typo.fix.md").unlink()
    patched = f"# Changelog\n\n## Unreleased\n\n## 1.2.1 - 01 SEP 2026\n\n- Fixed.\n\n{_RELEASE}"
    root.write_bytes(patched.encode())
    packaged.write_bytes(patched.encode())
    (tmp_path / "VERSION").write_text("1.2.1\n", encoding="utf-8")
    checker.validate(root, packaged, target="1.2.1")
