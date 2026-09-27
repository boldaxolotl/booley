"""Durability tests for unexpected endpoint exception diagnostics."""

from pathlib import Path

from booley.runtime.exception_diagnostics import _atomic_write_text, write_exception_diagnostic


def test_diagnostic_publish_failure_preserves_previous_file(tmp_path, monkeypatch) -> None:
    previous = tmp_path / "reviewer.invocation.error.log"
    previous.write_text("previous traceback", encoding="utf-8")

    def fail_replace(self: Path, target: Path) -> Path:
        del self, target
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", fail_replace)

    try:
        _atomic_write_text(previous, "new traceback")
    except OSError as exc:
        assert str(exc) == "replace failed"
    else:
        raise AssertionError("atomic replacement unexpectedly succeeded")

    assert previous.read_text(encoding="utf-8") == "previous traceback"
    assert sorted(tmp_path.iterdir()) == [previous]


def test_transcript_diagnostics_do_not_overwrite_prior_failures(tmp_path) -> None:
    transcript = tmp_path / "reviewer.jsonl"

    first = write_exception_diagnostic(
        ValueError("first failure"),
        endpoint_name="reviewer",
        invocation_id="same-invocation",
        transcript_path=transcript,
    )
    second = write_exception_diagnostic(
        ValueError("second failure"),
        endpoint_name="reviewer",
        invocation_id="same-invocation",
        transcript_path=transcript,
    )

    assert first is not None and second is not None and first != second
    assert "first failure" in first.read_text(encoding="utf-8")
    assert "second failure" in second.read_text(encoding="utf-8")
