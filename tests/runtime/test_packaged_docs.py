"""The packaged user-doc renderer: link rewriting and unclassified-link errors.

The copies are build output written by ``booley.runtime.build_stamp`` (#2495, #3658)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.runtime import packaged_docs

SOURCE = "docs/user/CONFIG.md"
MARKER = "(host doc, not shipped)"


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
    return root


def _packaged(root: Path) -> Path:
    return root / packaged_docs.PACKAGED_DOCS_RELATIVE_DIR


_EXTERNAL_AND_FENCED = (
    "[issue](https://github.com/boldaxolotl/Booley/issues/1) [fuse](https://fusesoc.net/x.md) "
    "[mail](mailto:a@b.example) [top](#top) escaped \\[x](SETUP.md) code `[y](SETUP.md)` "
    "![shot](https://example.com/a.png)\n"
    "```console\n[z](SETUP.md)\n```\n"
    "~~~\n[w](../user/USAGE.md)\n~~~\n"
    "[ftp](ftp://host/doc.md)\n"
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(
            "[a](TARGETS.md#submodules) [b](../user/TESTS.md) "
            "[c](../internals/FLOW_REPORTS.md#reports) "
            "[d](https://booley.dev/docs/flow-sim/#pre-sim-commands) "
            "[e](https://github.com/boldaxolotl/Booley/blob/main/docs/user/COVERAGE.md#x) "
            "[f](<CONFIG.md> 'title') [g](CONFIG.md#own-anchor)",
            "[a](TARGETS.md#submodules) [b](TESTS.md) [c](FLOW_REPORTS.md#reports) "
            "[d](FLOW-SIM.md#pre-sim-commands) [e](COVERAGE.md#x) "
            "[f](<CONFIG.md> 'title') [g](CONFIG.md#own-anchor)",
            id="shipped-docs-become-sibling-links",
        ),
        pytest.param(
            "[Setup](SETUP.md#first) [roadmap](../internals/ROADMAP.md) "
            "[doctor](https://booley.dev/docs/doctor/#checks) "
            '[`[models]`](STEALTH.md "Stealth") done',
            f"Setup {MARKER} roadmap {MARKER} doctor {MARKER} `[models]` {MARKER} done",
            id="unshipped-docs-become-marked-plain-text",
        ),
        pytest.param(
            _EXTERNAL_AND_FENCED, _EXTERNAL_AND_FENCED, id="external-anchor-and-fenced-unchanged"
        ),
        pytest.param(
            "see [the setup\nguide](SETUP.md) and [the config\nreference](CONFIG.md#x) now",
            f"see the setup\nguide {MARKER} and [the config\nreference](CONFIG.md#x) now",
            id="wrapped-link-text-is-rewritten",
        ),
    ],
)
def test_render_rewrites_links(text: str, expected: str) -> None:
    assert _render(text) == expected


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
