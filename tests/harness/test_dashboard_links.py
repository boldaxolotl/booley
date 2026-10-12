"""Published Dashboard paths: which text is clickable and how a path is opened."""

from __future__ import annotations

import subprocess

from booley.config.editor import editor_for_command
from booley.harness.dashboard.links import linked_text, open_in_editor


def test_only_whole_line_published_paths_are_clickable():
    detail = "Artifacts:\n/a/report.json\nResult: see /a/report.json\n/a/unpublished.log"
    text = linked_text(detail, ["/a/report.json", "/a/with 'quote.log"])
    assert str(text) == detail
    spans = [(detail[span.start : span.end], span.style.meta["@click"]) for span in text.spans]
    assert spans == [("/a/report.json", "app.open_path('/a/report.json')")]
    quoted = linked_text("/a/with 'quote.log", ["/a/with 'quote.log"])
    assert quoted.spans[0].style.meta["@click"] == 'app.open_path("/a/with \'quote.log")'


def test_open_in_editor_launches_detached_and_reports_each_failure(tmp_path, monkeypatch):
    # Built from tmp_path so the path is absolute on every platform.
    report = str(tmp_path / "report.json")
    calls = []

    def spawn(argv, **kwargs):
        calls.append((argv, kwargs))

    editor = editor_for_command("code")
    assert open_in_editor(report, editor=editor, spawn=spawn) == ""
    argv, kwargs = calls[0]
    assert argv == ["code", "--goto", report]
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs.get("start_new_session") or kwargs.get("creationflags")

    assert "Not an absolute path" in open_in_editor("report.json", editor=editor, spawn=spawn)
    monkeypatch.setattr("booley.harness.dashboard.links.resolve_editor", lambda: None)
    assert "No supported editor" in open_in_editor(report, spawn=spawn)

    def missing(argv, **kwargs):
        raise FileNotFoundError("code")

    assert "Editor launch failed" in open_in_editor(report, editor=editor, spawn=missing)
    assert len(calls) == 1
