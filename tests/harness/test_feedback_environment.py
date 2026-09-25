"""Harness composition of Feedback's optional Doctor observation."""

from argparse import ArgumentParser, Namespace

import pytest

from booley.feedback import cli as feedback_cli
from booley.feedback.findings import Finding, append
from booley.harness import booley, doctor_stamp, feedback_environment
from booley.runtime import project_dir as project_dir_mod


@pytest.mark.parametrize(
    ("stamp", "expected"),
    [
        ({"deep": True}, True),
        ({"deep": False}, False),
        ({"deep": 1}, None),
        ({"deep": "yes"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_resolve_feedback_environment_validates_deep(monkeypatch, tmp_path, stamp, expected):
    monkeypatch.setattr(feedback_environment.doctor_stamp, "load_stamp", lambda _path: stamp)

    env = feedback_environment.resolve_feedback_environment(tmp_path)

    assert env.doctor_deep_clean is expected


def test_resolve_feedback_environment_is_fail_soft_for_corrupt_stamp(tmp_path):
    path = doctor_stamp.stamp_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    assert feedback_environment.resolve_feedback_environment(tmp_path).doctor_deep_clean is None


@pytest.mark.parametrize(
    ("command", "reads_stamp"), [("report", True), ("export", True), ("list", False)]
)
def test_feedback_dispatch_reads_stamp_only_for_rendering_commands(
    monkeypatch, tmp_path, command, reads_stamp
):
    environment = object()
    reads: list[object] = []
    monkeypatch.setattr(booley, "feedback_storage_dir", lambda _root: tmp_path / "state")
    monkeypatch.setattr(
        booley,
        "resolve_feedback_environment",
        lambda path: reads.append(path) or environment,
    )
    monkeypatch.setattr(booley.feedback_cli, "run", lambda _args, _root, *, env: int(env is None))

    result = booley._cmd_feedback(Namespace(feedback_command=command), tmp_path)

    assert bool(reads) is reads_stamp
    assert result == int(not reads_stamp)


def test_feedback_report_renders_real_doctor_stamp(tmp_path, monkeypatch):
    monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
    project = tmp_path / "project"
    project_dir = project / ".booley_project"
    project_dir.mkdir(parents=True)
    (project_dir / "booley.toml").write_text(
        '[project]\nname = "project"\n',
        encoding="utf-8",
    )
    append(Finding(title="project note", bucket="project"), project_dir)
    assert doctor_stamp.record_clean_run(project_dir, project, deep=True) is not None
    parser = ArgumentParser()
    feedback_cli.add_subparser(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["feedback", "report"])

    project_dir_mod.reset_cache()
    try:
        assert booley._cmd_feedback(args, project) == 0
    finally:
        project_dir_mod.reset_cache()

    report = (project_dir / "SETUP-REPORT.md").read_text(encoding="utf-8")
    assert "| doctor --deep clean | yes |" in report
