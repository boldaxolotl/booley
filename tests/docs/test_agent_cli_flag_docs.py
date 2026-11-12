"""User docs spell the Agent CLI flags `--cli`, not the hidden `--app` alias."""

from pathlib import Path

from tests.shipped_docs_support import is_rendered_docs_output, rendered_docs_outside_user_docs

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_user_docs_use_cli_flag_spelling():
    for path in sorted((REPO_ROOT / "docs" / "user").glob("*.md")):
        assert "auth --app" not in path.read_text(encoding="utf-8"), path.name
    packaged = REPO_ROOT / "src" / "booley" / "data"
    for path in sorted(packaged.rglob("*.md")):
        if not is_rendered_docs_output(REPO_ROOT, path):  # build output; sources checked below
            assert "auth --app" not in path.read_text(encoding="utf-8"), path.name
    for source, text in rendered_docs_outside_user_docs(REPO_ROOT).items():
        assert "auth --app" not in text, source
    reference = (REPO_ROOT / "docs" / "user" / "CLI-REFERENCE.md").read_text(encoding="utf-8")
    assert "booley chat --cli codex" in reference
    assert "booley auth --cli codex" in reference
