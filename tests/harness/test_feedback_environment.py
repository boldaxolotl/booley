"""Harness composition of Feedback's optional Doctor observation."""

from argparse import Namespace

import pytest

from booley.harness import booley, feedback_environment


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


def test_resolve_feedback_environment_is_fail_soft(monkeypatch, tmp_path):
    def unreadable(_path):
        raise OSError("unreadable")

    monkeypatch.setattr(feedback_environment.doctor_stamp, "load_stamp", unreadable)

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
