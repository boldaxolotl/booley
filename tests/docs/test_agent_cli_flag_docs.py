"""User docs spell the Agent CLI flags `--cli`, not the hidden `--app` alias."""

from pathlib import Path

from tests.shipped_docs_support import rendered_docs_outside_user_docs
from tests.source_checkout_support import source_checkout_files

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_user_docs_use_cli_flag_spelling():
    for path in sorted((REPO_ROOT / "docs" / "user").glob("*.md")):
        assert "auth --app" not in path.read_text(encoding="utf-8"), path.name
    for path in source_checkout_files(REPO_ROOT, "src/booley/data"):
        if path.suffix == ".md":  # Git-ignored rendered docs are checked via their sources below
            assert "auth --app" not in path.read_text(encoding="utf-8"), path.name
    for source, text in rendered_docs_outside_user_docs(REPO_ROOT).items():
        assert "auth --app" not in text, source
    reference = (REPO_ROOT / "docs" / "user" / "CLI-REFERENCE.md").read_text(encoding="utf-8")
    assert "booley chat --cli codex" in reference
    assert "booley auth --cli codex" in reference
