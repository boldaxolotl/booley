import argparse
from datetime import date

import pytest

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity
from booley.audit.doctor_waivers import (
    DoctorWaiverError,
    load_doctor_waivers,
    warning,
)
from booley.harness import doctor, web_isolation
from booley.presentation import colors
from booley.runtime import runtime_context
from tests.harness.test_doctor import _patch_environment, _write_project


def _write(tmp_path, body: str) -> None:
    (tmp_path / "doctor-waivers.toml").write_text(body, encoding="utf-8", newline="\n")


def test_exact_subject_beats_check_wide_waiver(tmp_path):
    _write(
        tmp_path,
        """
version = 1

[[waiver]]
check = "sim.trace-unavailable"
reason = "All trace gaps are accepted."
permanent = true

[[waiver]]
check = "sim.trace-unavailable"
subject = "sim_fast"
reason = "This target uses the generated main."
expires = 2027-01-01
""",
    )
    waivers = load_doctor_waivers(tmp_path, today=date(2026, 8, 4))

    matched = waivers.match(warning("sim.trace-unavailable", "cannot trace", subject="sim_fast"))

    assert matched is not None
    assert matched.subject == "sim_fast"
    assert [item.subject for item in waivers.unused()] == [None]


def test_expired_waiver_does_not_match(tmp_path):
    _write(
        tmp_path,
        """
version = 1

[[waiver]]
check = "project.memory-image-untracked"
subject = "firmware.hex"
reason = "Generated during setup."
expires = 2026-08-03
""",
    )
    waivers = load_doctor_waivers(tmp_path, today=date(2026, 8, 4))

    assert waivers.active == ()
    assert len(waivers.expired) == 1
    assert (
        waivers.match(warning("project.memory-image-untracked", "missing", subject="firmware.hex"))
        is None
    )


@pytest.mark.parametrize(
    "body, fragment",
    [
        ("version = 2\n", "version must"),
        ("version = 1.0\n", "version must"),
        ("version = 1\nextra = true\n", "unknown top-level"),
        (
            'version = 1\n[[waiver]]\ncheck = "Bad ID"\nreason = "x"\npermanent = true\n',
            "lowercase",
        ),
        (
            'version = 1\n[[waiver]]\ncheck = "sim.trace"\nreason = "x"\n',
            "exactly one",
        ),
        (
            'version = 1\n[[waiver]]\ncheck = "sim.trace"\nreason = "x"\n'
            "expires = 2027-01-01\npermanent = true\n",
            "exactly one",
        ),
        (
            'version = 1\n[[waiver]]\ncheck = "sim.trace"\nreason = "x"\n'
            "expires = 2027-01-01T00:00:00Z\n",
            "TOML date",
        ),
        (
            'version = 1\n[[waiver]]\ncheck = "sim.trace"\nsubject = "sim_a"\n'
            'reason = "one"\npermanent = true\n'
            '[[waiver]]\ncheck = "sim.trace"\nsubject = "sim_a"\n'
            'reason = "two"\npermanent = true\n',
            "duplicate",
        ),
    ],
)
def test_invalid_file_fails_loud(tmp_path, body, fragment):
    _write(tmp_path, body)

    with pytest.raises(DoctorWaiverError, match=fragment):
        load_doctor_waivers(tmp_path, today=date(2026, 8, 4))


def test_reporter_dedupes_by_check_subject_and_key(capsys):
    # Private: no fixture Doctor run emits two same-key warnings without replacing Booley's own
    # image checks, and the reporter is the unit that owns the deduplication.
    reporter = doctor._Reporter.create()
    first = DiagnosticFinding(
        Severity.WARN, "first wording", "repair", "project.example", "target", "same"
    )
    duplicate = DiagnosticFinding(
        Severity.WARN, "new wording", "repair", "project.example", "target", "same"
    )
    other = DiagnosticFinding(
        Severity.WARN, "other subject", "repair", "project.example", "elsewhere", "same"
    )

    reporter.diagnostics(DiagnosticReport((first, duplicate, other)))

    output = capsys.readouterr().out
    assert "first wording" in output
    assert "other subject" in output
    assert "new wording" not in output
    result = reporter.result(0)
    assert result.counts["warn"] == 2
    assert result.findings == (
        DiagnosticFinding(Severity.WARN, "first wording", "repair", "project.example", "target"),
        DiagnosticFinding(
            Severity.WARN, "other subject", "repair", "project.example", "elsewhere"
        ),
    )


def test_doctor_subject_waiver_keeps_the_sibling_warning(tmp_path, monkeypatch, capsys):
    booley_dir = _write_project(tmp_path)
    # Stealth requires both entries in the exclude file, so each subject yields its own finding.
    with (booley_dir / "booley.toml").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write("\n[stealth]\nenabled = true\n")
    (tmp_path / ".git" / "info" / "exclude").write_bytes(b"")
    _write(
        booley_dir,
        """
version = 1
[[waiver]]
check = "project.git-excludes-missing"
subject = ".booley"
reason = "The project hides .booley another way."
permanent = true
""",
    )
    _patch_environment(monkeypatch, tmp_path, booley_dir)

    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)

    excludes = {
        f.subject: f.severity
        for f in result.findings
        if f.check_id == "project.git-excludes-missing"
    }
    assert excludes == {".devcontainer": Severity.WARN, ".booley": Severity.WAIVED}
    output = colors.strip_ansi(capsys.readouterr().out)
    assert "  !  git info/exclude missing Booley entry: .devcontainer" in output
    assert "  ~  git info/exclude missing Booley entry: .booley" in output
    assert "project.git-excludes-missing:.booley" in output
    assert "→ reason: The project hides .booley another way." in output
    assert result.counts["waived"] == 1


def test_doctor_waives_one_target_s_untagged_harness(tmp_path, monkeypatch):
    """A sim Target's untagged C++ harness is a waivable finding with the Target as subject."""
    booley_dir = _write_project(tmp_path)
    (tmp_path / "main.cpp").write_text("int main() { return 0; }\n", encoding="utf-8")
    (tmp_path / "cpp.core").write_text(
        "CAPI=2:\nname: ::cpp_harness:0\nfilesets:\n"
        "  rtl: {files: [rtl/dut.sv], file_type: systemVerilogSource}\n"
        "  harness: {files: [main.cpp], file_type: cppSource}\n"
        "targets:\n  sim_cpp:\n    flow: sim\n    flow_options: {tool: verilator}\n"
        "    filesets: [rtl, harness]\n    toplevel: dut\n",
        encoding="utf-8",
        newline="\n",
    )
    _write(
        booley_dir,
        'version = 1\n[[waiver]]\ncheck = "sim.harness-untagged"\nsubject = "sim_cpp"\n'
        'reason = "Accepted for this fixture"\npermanent = true\n',
    )
    _patch_environment(monkeypatch, tmp_path, booley_dir)

    result = doctor.run_doctor_result(argparse.Namespace(deep=False), tmp_path)

    untagged = [
        (f.severity, f.subject) for f in result.findings if f.check_id == "sim.harness-untagged"
    ]
    assert untagged == [(Severity.WAIVED, "sim_cpp")]


def _run_inside_the_sandbox(monkeypatch, root, booley_dir) -> None:
    """Make Doctor run as inside the Sandbox, with healthy web isolation."""
    _patch_environment(monkeypatch, root, booley_dir)
    monkeypatch.setattr(runtime_context, "inside_session_runtime", lambda: True)
    # The egress probe only runs in a marked Sandbox; keep these tests off real DNS.
    monkeypatch.delenv("BOOLEY_CONTAINER", raising=False)
    monkeypatch.setattr(web_isolation, "policy_error", lambda: None)
