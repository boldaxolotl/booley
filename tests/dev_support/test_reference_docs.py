"""The packaged-copy tooling: link rewriting, the CHANGELOG mirror sync, and ``--check``.

The user-doc copies are build output (``booley.runtime.build_stamp``); only the CHANGELOG
mirror is still synchronized here (#2495, #3658)."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

from booley.dev_support import reference_docs
from booley.runtime import packaged_docs

SOURCE = "docs/user/CONFIG.md"
MARKER = "(host doc, not shipped)"
CHANGELOG_SOURCE = "CHANGELOG.md"
CHANGELOG_MIRROR = "src/booley/data/refs/CHANGELOG.md"


def _render(text: str, source: str = SOURCE) -> str:
    return packaged_docs.render_packaged_doc(source, text)


def _fake_repo(tmp_path: Path, overrides: dict[str, str] | None = None) -> Path:
    """Return a repository root holding every shipped source, with ``overrides`` applied."""
    root = tmp_path / "repo"
    for source in packaged_docs.SHIPPED_DOCS:
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
    return root / packaged_docs.PACKAGED_DOCS_RELATIVE_DIR


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
    assert "packaged CHANGELOG mirror is current" in capsys.readouterr().out


@pytest.mark.parametrize("state", ["stale", "missing"])
def test_check_reports_stale_changelog_mirror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str
) -> None:
    root = _fake_repo(tmp_path)
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


def test_check_covers_only_the_changelog_mirror(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The user-doc copies are build output: ``--check`` neither needs nor reads them (#3658)."""
    root = _fake_repo(tmp_path)
    assert not _packaged(root).exists()

    assert reference_docs.main(["--check"], root=root) == 0
    assert capsys.readouterr().out == ""

    # A leftover rendered copy, stale or orphaned, is not this check's business.
    _packaged(root).mkdir(parents=True)
    (_packaged(root) / "CONFIG.md").write_text("hand edit\n", encoding="utf-8")
    (_packaged(root) / "RETIRED.md").write_text("old\n", encoding="utf-8")

    assert reference_docs.main(["--check"], root=root) == 0
    assert capsys.readouterr().out == ""


def test_check_prints_the_entry_points_own_fix_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _fake_repo(tmp_path)
    (root / CHANGELOG_MIRROR).write_bytes(b"hand edit\n")

    assert reference_docs.main(["--check"], root=root, sync_command="the-fix") == 1

    assert "packaged CHANGELOG mirror is out of date; run the-fix" in capsys.readouterr().out


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
    with pytest.raises(packaged_docs.UnclassifiedLinkError) as raised:
        _render(text)

    message = str(raised.value)
    assert SOURCE in message
    assert fragment in message


def test_renderer_alone_leaves_a_reference_style_link_unchanged() -> None:
    """The stdlib renderer has no cross-check; ``tests/docs/test_shipped_doc_link_forms.py`` owns it."""
    text = "[x][ref]\n\n[ref]: SETUP.md\n"

    assert _render(text) == text
    assert packaged_docs.link_destinations(text) == []


def test_unclassified_link_leaves_the_packaged_directory_untouched(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    packaged_docs.write_packaged_docs(root)
    before = {path.name: path.read_bytes() for path in _packaged(root).iterdir()}
    (root / "docs/user/TESTS.md").write_bytes(b"[new](NEW.md)\n")
    (root / SOURCE).write_bytes(b"# changed\n")

    with pytest.raises(packaged_docs.UnclassifiedLinkError, match=r"NEW\.md"):
        packaged_docs.write_packaged_docs(root)

    assert {path.name: path.read_bytes() for path in _packaged(root).iterdir()} == before


def test_missing_shipped_source_names_the_repository_path(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    (root / "docs/user/TESTS.md").unlink()

    with pytest.raises(
        packaged_docs.MissingShippedDocError,
        match=r"shipped doc source is missing: docs/user/TESTS.md",
    ):
        packaged_docs.expected_copies(root)


def test_orphan_directory_is_removed_not_fatal(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path)
    packaged_docs.write_packaged_docs(root)
    orphan = root / packaged_docs.PACKAGED_DOCS_RELATIVE_DIR / "stale_dir"
    orphan.mkdir()
    (orphan / "inner.md").write_bytes(b"old\n")

    assert packaged_docs.write_packaged_docs(root) == ["stale_dir"]
    assert not orphan.exists()


def test_atomic_write_failure_removes_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    packaged = tmp_path / "packaged.md"
    packaged.write_text("original\n", encoding="utf-8")

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(Path, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        packaged_docs.atomic_write(packaged, b"replacement\n")

    assert packaged.read_text(encoding="utf-8") == "original\n"
    assert list(tmp_path.iterdir()) == [packaged]
