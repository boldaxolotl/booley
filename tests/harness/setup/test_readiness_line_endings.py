"""Exact finding text for the read-only line-ending inspection owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.audit.diagnostic_results import Severity
from booley.harness.setup import readiness
from booley.harness.setup.line_endings import (
    LineEndingActionResult,
    LineEndingObservation,
    LineEndingObservationCode,
    LineEndingReport,
    LineEndingRepository,
    LineEndingStatus,
    RepositoryDiscoveryFailure,
    RepositoryLineEndingReport,
)

Code = LineEndingObservationCode
_ROLE = "project-checkout"
_UNREADABLE = "git.line-endings-unreadable"

_CRLF_FIX = "booley init   (automatically repairs a clean tree; commit or stash first)"
_AUTOCRLF_TRUE = (
    "core.autocrlf=true — the tree is LF today, but the next clone or checkout "
    "will re-create it with CRLF and break Goal Mode"
)
_AUTOCRLF_UNSET = (
    "core.autocrlf is not set locally — the effective value is false today, but a "
    "global config change can re-create CRLF checkouts"
)

# (id, observations, actions, expected (severity, message, fix, check_id, subject) with
# "{id}" standing for the repository identity). Literal text from the pre-move Doctor.
_CASES = [
    (
        "crlf-mismatch",
        (LineEndingObservation(Code.CRLF_MISMATCH, count=3),),
        (),
        (
            Severity.FAIL,
            "{id}: 3 tracked file(s) are checked out with CRLF — the Sandbox sees every "
            "one as modified, which breaks the dirty-tree check, protected inputs, and "
            "worktrees",
            _CRLF_FIX,
            None,
            None,
        ),
    ),
    (
        "stale-index",
        (LineEndingObservation(Code.STALE_INDEX),),
        (),
        (
            Severity.FAIL,
            "{id}: tracked files have stale Git index metadata after line-ending repair — "
            "status reports modifications although staged and unstaged diffs are empty",
            "booley init   (refreshes the affected tracked index entries)",
            None,
            None,
        ),
    ),
    (
        "autocrlf-effective-true",
        (LineEndingObservation(Code.AUTOCRLF_EFFECTIVE_TRUE),),
        (),
        (
            Severity.WARN,
            "{id}: " + _AUTOCRLF_TRUE,
            "git -C {root} config --local core.autocrlf false   (or re-run `booley init`)",
            "git.autocrlf-risk",
            _ROLE,
        ),
    ),
    (
        "autocrlf-not-pinned",
        (LineEndingObservation(Code.AUTOCRLF_NOT_PINNED),),
        (),
        (
            Severity.WARN,
            "{id}: " + _AUTOCRLF_UNSET,
            "git -C {root} config --local core.autocrlf false   (or re-run `booley init`)",
            "git.autocrlf-risk",
            _ROLE,
        ),
    ),
    (
        "autocrlf-unreadable",
        (LineEndingObservation(Code.AUTOCRLF_UNREADABLE),),
        (),
        (
            Severity.WARN,
            "line endings: {id}: could not read core.autocrlf as a Git Boolean",
            "",
            _UNREADABLE,
            _ROLE,
        ),
    ),
    (
        "local-autocrlf-unreadable",
        (LineEndingObservation(Code.LOCAL_AUTOCRLF_UNREADABLE),),
        (),
        (
            Severity.WARN,
            "line endings: {id}: could not read repo-local core.autocrlf",
            "",
            _UNREADABLE,
            _ROLE,
        ),
    ),
    (
        "eol-scan-unreadable",
        (LineEndingObservation(Code.EOL_SCAN_UNREADABLE),),
        (),
        (
            Severity.WARN,
            "line endings: {id}: could not read `git ls-files --eol`",
            "",
            _UNREADABLE,
            _ROLE,
        ),
    ),
    (
        "status-unreadable-with-detail",
        (LineEndingObservation(Code.STATUS_UNREADABLE, detail="git diff timed out"),),
        (),
        (
            Severity.WARN,
            "line endings: {id}: git diff timed out",
            "",
            _UNREADABLE,
            _ROLE,
        ),
    ),
    (
        "status-unreadable-without-detail",
        (LineEndingObservation(Code.STATUS_UNREADABLE),),
        (),
        (
            Severity.WARN,
            "line endings: {id}: inspection was incomplete",
            "",
            _UNREADABLE,
            _ROLE,
        ),
    ),
    (
        "pass",
        (),
        (),
        (
            Severity.PASS,
            "{id}: working tree is container-safe (no CRLF checkouts, autocrlf off)",
            "",
            None,
            None,
        ),
    ),
]


def _inspect(root: Path, report: LineEndingReport, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(readiness, "reconcile_project_line_endings", lambda *_a, **_k: report)
    return readiness.inspect_line_endings(readiness.LineEndingInspectionRequest(root))


@pytest.mark.parametrize(
    ("observations", "actions", "expected"),
    [pytest.param(*case[1:], id=case[0]) for case in _CASES],
)
def test_line_ending_findings_keep_exact_text(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    observations: tuple[LineEndingObservation, ...],
    actions: tuple[LineEndingActionResult, ...],
    expected: tuple[Severity, str, str, str | None, str | None],
) -> None:
    repository = LineEndingRepository(_ROLE, tmp_path)
    report = LineEndingReport(
        LineEndingStatus.UNSAFE,
        (RepositoryLineEndingReport(repository, LineEndingStatus.UNSAFE, observations, actions),),
        (),
    )
    identity = f"Project Root ({tmp_path})"
    severity, message, fix, check_id, subject = expected

    findings = _inspect(tmp_path, report, monkeypatch).findings

    assert [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in findings] == [
        (
            severity,
            message.format(id=identity),
            fix.format(root=tmp_path),
            check_id,
            subject,
        )
    ]


def test_not_applicable_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    report = LineEndingReport(LineEndingStatus.NOT_APPLICABLE, (), ())

    findings = _inspect(tmp_path, report, monkeypatch).findings

    assert [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in findings] == [
        (Severity.SKIP, "line endings: Project Root is not a git repo", "", None, None)
    ]


def test_discovery_failure_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failure = RepositoryDiscoveryFailure("project-data", tmp_path, "not a git repository")
    report = LineEndingReport(LineEndingStatus.UNSAFE, (), (failure,))

    findings = _inspect(tmp_path, report, monkeypatch).findings

    assert [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in findings] == [
        (
            Severity.WARN,
            f"line endings: could not inspect Booley Directory ({tmp_path}): not a git repository",
            "",
            _UNREADABLE,
            "project-data",
        )
    ]
