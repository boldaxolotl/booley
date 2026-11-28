"""Durability tests for unexpected endpoint exception diagnostics."""

from booley.runtime.exception_diagnostics import write_exception_diagnostic


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


def test_report_diagnostic_sanitizes_untrusted_path_components(tmp_path) -> None:
    path = write_exception_diagnostic(
        ValueError("failure"),
        endpoint_name="../../reviewer",
        invocation_id="../outside/report",
        report_dir=tmp_path,
    )

    assert path is not None
    assert path.is_relative_to(tmp_path / ".endpoint-errors")
    assert ".." not in path.relative_to(tmp_path).parts
    assert "failure" in path.read_text(encoding="utf-8")


def test_report_diagnostic_for_a_removed_checkout_prints_instead(tmp_path, capsys) -> None:
    checkout = tmp_path / "gone"

    path = write_exception_diagnostic(
        ValueError("late failure"),
        endpoint_name="lint",
        invocation_id="run",
        report_dir=checkout / ".booley" / "flow-reports",
    )

    assert path is None
    assert not checkout.exists()
    assert "ValueError: late failure" in capsys.readouterr().err
