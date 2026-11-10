"""The packaged user-doc sync: allowlist, link rewriting, and ``--check`` (issue #2495)."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from booley.dev_support import reference_docs

SOURCE = "docs/user/CONFIG.md"
MARKER = "(host doc, not shipped)"
CHANGELOG_SOURCE = "CHANGELOG.md"
CHANGELOG_MIRROR = "src/booley/data/refs/CHANGELOG.md"


def _render(text: str, source: str = SOURCE) -> str:
    return reference_docs.render_packaged_doc(source, text)


def _fake_repo(tmp_path: Path, overrides: dict[str, str] | None = None) -> Path:
    """Return a repository root holding every shipped source, with ``overrides`` applied."""
    root = tmp_path / "repo"
    for source in reference_docs.SHIPPED_DOCS:
        path = root / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"# {path.name}\n".encode())
    for source, text in (overrides or {}).items():
        (root / source).write_bytes(text.encode())
    # A current checkout: the packaged CHANGELOG mirror already matches its source.
    mirror = root / CHANGELOG_MIRROR
    mirror.parent.mkdir(parents=True, exist_ok=True)
    (root / CHANGELOG_SOURCE).write_bytes(b"# Changelog\n")
    mirror.write_bytes(b"# Changelog\n")
    return root


def _packaged(root: Path) -> Path:
    return root / reference_docs.PACKAGED_DOCS_RELATIVE_DIR


def test_sync_writes_one_flat_copy_per_allowlisted_doc(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)

    changed = reference_docs.sync_packaged_docs(root)

    expected = sorted(Path(source).name for source in reference_docs.SHIPPED_DOCS)
    assert sorted(changed) == expected
    assert sorted(path.name for path in _packaged(root).iterdir()) == expected
    assert len(expected) == len(reference_docs.SHIPPED_DOCS) == 13
    assert (_packaged(root) / "CONFIG.md").read_text(encoding="utf-8") == "# CONFIG.md\n"


def test_sync_mirrors_changelog_byte_for_byte(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path)
    # A shipped-doc link, non-ASCII text and a CRLF line: copied verbatim, never rewritten.
    changelog = "# Changelog\n[x](docs/user/SETUP.md) caf\u00e9\r\nlast\n".encode()
    (root / CHANGELOG_SOURCE).write_bytes(changelog)
    mirror = root / CHANGELOG_MIRROR

    assert reference_docs.main([], root=root) == 0

    assert mirror.read_bytes() == changelog
    assert "src/booley/data/refs/CHANGELOG.md" in capsys.readouterr().out
    assert reference_docs.main([], root=root) == 0
    assert "packaged docs are current" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["stale", "missing"])
def test_check_reports_stale_changelog_mirror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    root = _fake_repo(tmp_path)
    reference_docs.sync_packaged_docs(root)
    mirror = root / CHANGELOG_MIRROR
    if state == "stale":
        mirror.write_bytes(b"hand edit\n")
    else:
        mirror.unlink()
    before = mirror.read_bytes() if mirror.exists() else None

    assert reference_docs.main(["--check"], root=root) == 1

    output = capsys.readouterr().out
    assert f"src/booley/data/refs/CHANGELOG.md: packaged copy is {state}" in output
    assert "python -m booley.dev_support.reference_docs" in output
    assert (mirror.read_bytes() if mirror.exists() else None) == before

    assert reference_docs.main([], root=root) == 0
    capsys.readouterr()
    assert reference_docs.main(["--check"], root=root) == 0
    assert capsys.readouterr().out == ""


def test_check_prints_the_entry_points_own_fix_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path)
    reference_docs.sync_packaged_docs(root)
    (root / CHANGELOG_MIRROR).write_bytes(b"hand edit\n")

    assert reference_docs.main(["--check"], root=root, sync_command="the-fix") == 1

    assert "packaged docs are out of date; run the-fix" in capsys.readouterr().out


def test_launcher_names_itself_as_the_fix_command(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = Path(__file__).resolve().parents[2] / ".github/scripts/sync_packaged_copies.py"
    seen: dict[str, str] = {}

    def fake_main(argv: list[str] | None = None, *, sync_command: str) -> int:
        seen["sync_command"] = sync_command
        return 0

    monkeypatch.setattr(reference_docs, "main", fake_main)
    monkeypatch.setattr("sys.dont_write_bytecode", True)

    namespace = runpy.run_path(str(launcher))
    assert namespace["main"]() == 0

    assert seen["sync_command"] == "python3 .github/scripts/sync_packaged_copies.py"


def test_second_sync_is_a_no_op(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _fake_repo(tmp_path)
    assert reference_docs.main([], root=root) == 0
    assert "updated packaged docs" in capsys.readouterr().out
    before = {path.name: path.stat().st_mtime_ns for path in _packaged(root).iterdir()}

    assert reference_docs.sync_packaged_docs(root) == []
    assert reference_docs.main([], root=root) == 0

    assert "packaged docs are current" in capsys.readouterr().out
    assert {path.name: path.stat().st_mtime_ns for path in _packaged(root).iterdir()} == before


def test_links_to_shipped_docs_become_sibling_links() -> None:
    text = (
        "[a](TARGETS.md#submodules) [b](../user/TESTS.md) "
        "[c](../internals/FLOW_REPORTS.md#reports) "
        "[d](https://booley.dev/docs/flow-sim/#pre-sim-commands) "
        "[e](https://github.com/boldaxolotl/Booley/blob/main/docs/user/COVERAGE.md#x) "
        "[f](<CONFIG.md> 'title') [g](CONFIG.md#own-anchor)"
    )

    assert _render(text) == (
        "[a](TARGETS.md#submodules) [b](TESTS.md) [c](FLOW_REPORTS.md#reports) "
        "[d](FLOW-SIM.md#pre-sim-commands) [e](COVERAGE.md#x) "
        "[f](<CONFIG.md> 'title') [g](CONFIG.md#own-anchor)"
    )


def test_links_to_unshipped_docs_become_marked_plain_text() -> None:
    text = (
        "[Setup](SETUP.md#first) [roadmap](../internals/ROADMAP.md) "
        "[doctor](https://booley.dev/docs/doctor/#checks) "
        '[`[models]`](STEALTH.md "Stealth") done'
    )

    assert _render(text) == (
        f"Setup {MARKER} roadmap {MARKER} doctor {MARKER} `[models]` {MARKER} done"
    )


def test_external_links_anchors_and_fenced_code_are_unchanged() -> None:
    text = (
        "[issue](https://github.com/boldaxolotl/Booley/issues/1) [fuse](https://fusesoc.net/x.md) "
        "[mail](mailto:a@b.example) [top](#top) escaped \\[x](SETUP.md) code `[y](SETUP.md)` "
        "![shot](https://example.com/a.png)\n"
        "```console\n[z](SETUP.md)\n```\n"
        "~~~\n[w](../user/USAGE.md)\n~~~\n"
        "[ftp](ftp://host/doc.md)\n"
    )

    assert _render(text) == text


def test_wrapped_link_text_is_rewritten() -> None:
    text = "see [the setup\nguide](SETUP.md) and [the config\nreference](CONFIG.md#x) now"

    assert _render(text) == (
        f"see the setup\nguide {MARKER} and [the config\nreference](CONFIG.md#x) now"
    )


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[x](NEW.md)", "docs/user/NEW.md"),
        ("[x](https://booley.dev/docs/nope/)", "not a user doc"),
        ("[x](https://booley.dev/videos/)", "not a user doc"),
        (
            "[x](https://github.com/boldaxolotl/booley/blob/main/src/booley/x.py)",
            "src/booley/x.py",
        ),
        ("[x](../../../outside.md)", "escapes the repository"),
        ("[x](/abs/path.md)", "use a relative path"),
        ("[x](SETUP.md?raw=1)", "use a relative path"),
        ("![x](assets/shot.png)", "docs/user/assets/shot.png"),
        ("[a ![i](assets/i.png) b](SETUP.md)", "nested in another doc link"),
    ],
)
def test_unclassified_link_names_file_link_and_remedy(text: str, fragment: str) -> None:
    with pytest.raises(reference_docs.UnclassifiedLinkError) as raised:
        _render(text)

    message = str(raised.value)
    assert SOURCE in message
    assert fragment in message


def test_unclassified_link_fails_sync_and_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path, {SOURCE: "# Config\n[new](NEW.md#part)\n"})

    assert reference_docs.main([], root=root) == 1
    sync_output = capsys.readouterr().out
    assert reference_docs.main(["--check"], root=root) == 1
    check_output = capsys.readouterr().out

    for output in (sync_output, check_output):
        assert SOURCE in output
        assert "NEW.md" in output
        assert "add the target to SHIPPED_DOCS or PLAIN_TEXT_TARGETS" in output
    assert not _packaged(root).exists()


def test_link_form_the_scanner_misses_fails() -> None:
    forms = [
        "[x][ref]\n\n[ref]: SETUP.md\n",
        "<https://booley.dev/docs/setup/>\n",
        "text\n\n    [x](SETUP.md)\n",
        '<a href="SETUP.md">x</a>\n',
        'Inline <a href="SETUP.md">x</a> link.\n',
    ]
    for text in forms:
        with pytest.raises(reference_docs.UnclassifiedLinkError, match="cannot classify"):
            _render(text)


def test_check_fails_on_stale_copy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _fake_repo(tmp_path)
    reference_docs.sync_packaged_docs(root)
    (_packaged(root) / "TARGETS.md").write_text("hand edit\n", encoding="utf-8")
    (_packaged(root) / "TESTS.md").unlink()

    assert reference_docs.main(["--check"], root=root) == 1

    output = capsys.readouterr().out
    assert "TARGETS.md: packaged copy is stale" in output
    assert "TESTS.md: packaged copy is missing" in output
    assert "python -m booley.dev_support.reference_docs" in output


def test_check_fails_on_orphaned_packaged_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path)
    reference_docs.sync_packaged_docs(root)
    orphan = _packaged(root) / "RETIRED.md"
    orphan.write_text("old\n", encoding="utf-8")

    assert reference_docs.main(["--check"], root=root) == 1
    assert "RETIRED.md: orphaned packaged file" in capsys.readouterr().out

    assert reference_docs.sync_packaged_docs(root) == ["RETIRED.md"]
    assert not orphan.exists()


def test_check_passes_after_fresh_sync(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _fake_repo(tmp_path, {SOURCE: "# Config\n[s](SETUP.md) [t](TARGETS.md)\n"})
    reference_docs.sync_packaged_docs(root)

    assert reference_docs.main(["--check"], root=root) == 0
    assert capsys.readouterr().out == ""
    assert (_packaged(root) / "CONFIG.md").read_text(encoding="utf-8") == (
        f"# Config\ns {MARKER} [t](TARGETS.md)\n"
    )


def test_atomic_write_failure_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "packaged.md"
    packaged.write_text("original\n", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        reference_docs._atomic_write(packaged, b"replacement\n")

    assert packaged.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.iterdir()) == [packaged]
