import subprocess
from datetime import date

import pytest

from booley.harness import doctor
from booley.harness.doctor_waivers import (
    DoctorWaiverError,
    load_doctor_waivers,
    warning,
)


def _write(tmp_path, body: str) -> None:
    (tmp_path / "doctor-waivers.toml").write_text(body, encoding="utf-8")


def test_missing_file_is_empty(tmp_path):
    waivers = load_doctor_waivers(tmp_path)

    assert waivers.active == ()
    assert waivers.expired == ()


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
    ],
)
def test_invalid_file_fails_loud(tmp_path, body, fragment):
    _write(tmp_path, body)

    with pytest.raises(DoctorWaiverError, match=fragment):
        load_doctor_waivers(tmp_path, today=date(2026, 8, 4))


def test_duplicate_scope_is_rejected(tmp_path):
    _write(
        tmp_path,
        """
version = 1
[[waiver]]
check = "sim.trace"
subject = "sim_a"
reason = "one"
permanent = true
[[waiver]]
check = "sim.trace"
subject = "sim_a"
reason = "two"
permanent = true
""",
    )

    with pytest.raises(DoctorWaiverError, match="duplicate"):
        load_doctor_waivers(tmp_path)


def test_reporter_keeps_waived_finding_visible(tmp_path, capsys):
    _write(
        tmp_path,
        """
version = 1
[[waiver]]
check = "sim.trace-unavailable"
subject = "sim_fast"
reason = "The upstream harness deliberately has no trace switch."
permanent = true
""",
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(tmp_path))

    reporter.warn_(
        warning(
            "sim.trace-unavailable",
            "trace is unavailable",
            subject="sim_fast",
        )
    )

    output = capsys.readouterr().out
    assert "WAIVED  trace is unavailable [sim.trace-unavailable:sim_fast]" in output
    assert "reason: The upstream harness deliberately has no trace switch." in output
    assert reporter.counts["warn"] == 0
    assert reporter.counts["waived"] == 1


def test_reporter_dedupes_one_subject_but_not_another(capsys):
    reporter = doctor._Reporter.create()

    reporter.warn_(warning("sandbox.image-stale", "first", subject="a", dedupe="image"))
    reporter.warn_(warning("sandbox.image-stale", "duplicate", subject="a", dedupe="image"))
    reporter.warn_(warning("sandbox.image-stale", "other", subject="b", dedupe="image"))

    output = capsys.readouterr().out
    assert "first" in output
    assert "duplicate" not in output
    assert "other" in output
    assert reporter.counts["warn"] == 2


def test_subject_waiver_does_not_hide_sibling_finding(tmp_path, capsys):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _write(
        tmp_path,
        """
version = 1
[[waiver]]
check = "project.git-excludes-missing"
subject = ".booley_project"
reason = "The project uses the approved open footprint."
permanent = true
""",
    )
    reporter = doctor._Reporter.create(load_doctor_waivers(tmp_path))

    doctor._check_devcontainer_excludes(tmp_path, reporter.pass_, reporter.warn_)

    output = capsys.readouterr().out
    assert "WARN  git info/exclude missing Booley entry: .devcontainer" in output
    assert "WAIVED  git info/exclude missing Booley entry: .booley_project" in output
    assert reporter.counts["warn"] == 1
    assert reporter.counts["waived"] == 1
