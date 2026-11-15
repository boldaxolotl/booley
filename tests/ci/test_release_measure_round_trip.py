"""The measurement round trip on real Git repositories, with ``booley measure`` stubbed."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / ".github/scripts"))


def _trip() -> Any:
    """Import lazily: the module under test is not part of the test patch's base."""
    from release_validation import measure_round_trip

    return measure_round_trip


CONTRACT = ROOT / ".github/contracts/picorv32-demo.toml"
METRICS = {"lint_core": {"lint": {"errors_max": 0, "track": ["warnings"]}}}
ALREADY = "abcdef123456 is already Measured by run r-1; showing it (--again measures it again)"


def _completed(code: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["booley", "measure"], code, stdout, stderr)


def _document(
    sha: str,
    *,
    status: str = "completed",
    target: str = "lint_core",
    key: str = "errors_max",
    value: object = 0,
    verdict: str = "met",
    with_table: bool = True,
) -> dict[str, Any]:
    results = [{"key": key, "bound": 0, "value": value, "verdict": verdict, "detail": None}]
    tables = [{"identity": f"::x:0#{target}", "flow": "lint", "results": results}]
    return {
        "status": status,
        "run_id": "r-1",
        "commit": {"sha": sha},
        "tables": tables if with_table else [],
    }


def _head(path: Path) -> str:
    return _trip()._git(path, "rev-parse", "HEAD")


def _repository(path: Path, name: str, content: str = "x\n") -> Path:
    path.mkdir(parents=True)
    _trip()._git(path, "init", "--quiet", "-b", "main")
    (path / name).write_bytes(content.encode())
    _trip()._commit_all(path, "Initial")
    return path


class _Demo:
    """The demo source checkout and its Booley Directory, plus a scripted ``booley measure``."""

    def __init__(self, tmp_path: Path) -> None:
        self.source = _repository(tmp_path / "source", "README")
        self.state = _repository(tmp_path / "state", "booley.toml", "[project]\n")
        self.workspace = tmp_path / "workspace"
        self.workspace.mkdir()
        self.calls: list[tuple[Path, Path, tuple[str, ...]]] = []
        self.first: subprocess.CompletedProcess[str] | None = None
        self.second = _completed(0, "Measurement Report\n1 of 1 enforced met\n", ALREADY)

    def measure(
        self, project: Path, booley_dir: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((project, booley_dir, args))
        if args == ("--json",):
            return self.first or _completed(0, json.dumps(_document(_head(project))))
        return self.second

    def run(self) -> dict[str, Any]:
        return _trip().demo_leg(self.source, self.state, METRICS, self.workspace)


def _demo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Demo:
    scripted = _Demo(tmp_path)
    monkeypatch.setattr(_trip(), "_measure", scripted.measure)
    return scripted


def test_demo_round_trip_commits_contract_metrics_and_requires_a_measured_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _demo(tmp_path, monkeypatch)
    source_head, source_state_head = _head(demo.source), _head(demo.state)
    evidence = demo.run()

    clone, state = demo.workspace / "demo", demo.workspace / "demo" / ".booley"
    committed = _trip()._git(state, "show", "HEAD:metrics.toml")
    assert committed == _trip().render_metrics(METRICS).strip()
    assert "[lint_core.lint]" in committed and "errors_max = 0" in committed
    assert evidence == {
        "commit": _head(clone),
        "booley_directory_commit": _head(state),
        "run_id": "r-1",
        "verdict": "1 of 1 enforced met",
        "exit_code": 0,
        "measured": True,
    }
    assert [(call[0], call[1]) for call in demo.calls] == [(clone, state)] * 2
    assert [call[2] for call in demo.calls] == [("--json",), ()]
    # The caller's checkouts are only cloned.
    assert (_head(demo.source), _head(demo.state)) == (source_head, source_state_head)
    assert not (demo.state / "metrics.toml").exists()
    assert "/.booley" in (clone / ".git/info/exclude").read_text()


def test_demo_round_trip_commits_even_when_the_demo_declares_the_same_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _demo(tmp_path, monkeypatch)
    (demo.state / "metrics.toml").write_bytes(_trip().render_metrics(METRICS).encode())
    _trip()._commit_all(demo.state, "Declare the metrics already")
    before = _head(demo.state)
    evidence = demo.run()
    assert evidence["booley_directory_commit"] != before


def test_demo_round_trip_fails_when_measure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _demo(tmp_path, monkeypatch)
    for code in (1, 2):
        demo.first = _completed(code, "report on stdout", "reason on stderr")
        with pytest.raises(_trip().RoundTripError, match=f"exited {code}") as error:
            _trip().demo_leg(demo.source, demo.state, METRICS, _fresh(tmp_path))
        assert "report on stdout" in str(error.value)
        assert "reason on stderr" in str(error.value)


def test_demo_round_trip_fails_when_the_second_measure_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _demo(tmp_path, monkeypatch)
    demo.second = _completed(1, "", "unmet")
    with pytest.raises(_trip().RoundTripError, match=r"second measure.*exited 1"):
        demo.run()


def test_demo_round_trip_refuses_exit_zero_without_a_measured_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    demo = _demo(tmp_path, monkeypatch)
    head = _head(demo.source)
    cases = [
        (_completed(0, json.dumps(_document(head, status="stopped"))), "not Measured"),
        (_completed(0, json.dumps(_document(head, verdict="unmet"))), "not Measured"),
        (_completed(0, json.dumps(_document("0" * 40))), "measured '0000"),
        (_completed(0, "not json"), "unreadable run document"),
        (_completed(0, "[]"), "not an object"),
    ]
    for first, message in cases:
        demo.first = first
        with pytest.raises(_trip().RoundTripError, match=message):
            _trip().demo_leg(demo.source, demo.state, METRICS, _fresh(tmp_path))
    demo.first = None
    for second, message in [
        (_completed(0, "Measurement Report\nok\n", "ran again"), "not a Measured Commit"),
        (_completed(0, "", ALREADY), "no verdict line"),
        (_completed(0, "Measurement Report\n \n", ALREADY), "no verdict line"),
    ]:
        demo.second = second
        with pytest.raises(_trip().RoundTripError, match=message):
            _trip().demo_leg(demo.source, demo.state, METRICS, _fresh(tmp_path))


def _fresh(tmp_path: Path) -> Path:
    root = tmp_path / f"fresh-{len(list(tmp_path.glob('fresh-*')))}"
    root.mkdir()
    return root


class _Pins:
    """A scripted ``booley measure`` that records which commit each run could see."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str]] = []
        self.replies: list[subprocess.CompletedProcess[str]] = []

    def measure(
        self, project: Path, booley_dir: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        assert booley_dir == project / ".booley" and args == ("--json",)
        pin = _trip()._git(project, "rev-parse", "HEAD:rtl")
        self.seen.append((pin, _trip()._git(project / "rtl", "rev-parse", "HEAD")))
        assert (project / ".booley/metrics.toml").read_text() == (
            "[lint_pin.lint]\nerrors_max = 0\n"
        )
        return self.replies[len(self.seen) - 1]


def _good() -> subprocess.CompletedProcess[str]:
    return _completed(0, json.dumps(_document("a" * 40, target="lint_pin")))


def _bad(**changes: Any) -> subprocess.CompletedProcess[str]:
    document = _document("b" * 40, target="lint_pin", value=1, verdict="unmet")
    return _completed(changes.pop("code", 1), json.dumps(document | changes))


def _pins(monkeypatch: pytest.MonkeyPatch) -> _Pins:
    scripted = _Pins()
    monkeypatch.setattr(_trip(), "_measure", scripted.measure)
    return scripted


def test_submodule_leg_measures_the_pin_while_the_live_tree_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _pins(monkeypatch)
    pins.replies = [_good(), _bad()]
    evidence = _trip().submodule_leg(tmp_path)

    (first_pin, first_live), (second_pin, second_live) = pins.seen
    assert first_pin != first_live  # the live tree is ahead of the pin
    assert second_pin == first_live and second_live == first_pin  # the pin moved, the tree did not
    assert evidence == {
        "pinned": first_pin,
        "live": first_live,
        "exit_code": 0,
        "measured": True,
        "bad_pin": {"exit_code": 1, "errors": 1, "verdict": "unmet"},
    }
    # The pinned commit is the clean RTL; the other holds the lint error.
    ip = tmp_path / "ip"
    assert "undeclared_net" not in _trip()._git(ip, "show", f"{first_pin}:pin.sv")
    assert "undeclared_net" in _trip()._git(ip, "show", f"{first_live}:pin.sv")
    assert "default_nettype none" in _trip()._git(ip, "show", f"{first_live}:pin.sv")


def test_submodule_leg_fails_when_the_bad_pin_still_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _pins(monkeypatch)
    pins.replies = [_good(), _good()]
    with pytest.raises(_trip().RoundTripError, match=r"pin at the bad RTL.*exited 0"):
        _trip().submodule_leg(tmp_path)


def test_submodule_leg_fails_when_the_clean_pin_is_not_measured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pins = _pins(monkeypatch)
    pins.replies = [_bad(), _bad()]
    with pytest.raises(_trip().RoundTripError, match="pin at the clean RTL"):
        _trip().submodule_leg(tmp_path)


def test_submodule_leg_rejects_a_bad_pin_run_that_did_not_lint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = [
        (_bad(code=2), "exited 2"),
        (_bad(code=130), "exited 130"),
        (_bad(tables=[]), "did not complete a lint_pin lint table"),
        (_bad(status="stopped"), "did not complete"),
        (
            _completed(
                1, json.dumps(_document("b" * 40, target="other", value=1, verdict="unmet"))
            ),
            "did not complete a lint_pin lint table",
        ),
        (
            _completed(1, json.dumps(_document("b" * 40, target="lint_pin", key="x", value=1))),
            "no errors_max result",
        ),
        (
            _completed(
                1, json.dumps(_document("b" * 40, target="lint_pin", value=0, verdict="unmet"))
            ),
            "errors=0",
        ),
        (
            _completed(
                1, json.dumps(_document("b" * 40, target="lint_pin", value=1, verdict="error"))
            ),
            "verdict is 'error'",
        ),
        (_completed(1, "not json"), "unreadable run document"),
    ]
    pins = _pins(monkeypatch)
    for reply, message in cases:
        pins.seen.clear()
        pins.replies = [_good(), reply]
        with pytest.raises(_trip().RoundTripError, match=message):
            _trip().submodule_leg(_fresh(tmp_path))


def test_round_trip_runs_both_legs_on_the_contract_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def demo_leg(project: Path, state: Path, metrics: Any, workspace: Path) -> dict[str, Any]:
        seen.update(project=project, state=state, metrics=metrics, workspace=workspace)
        return {"demo": True}

    monkeypatch.setattr(_trip(), "demo_leg", demo_leg)
    monkeypatch.setattr(_trip(), "submodule_leg", lambda workspace: {"workspace": workspace.name})
    evidence = _trip().round_trip(
        project=tmp_path / "p", project_state=tmp_path / "s", contract=CONTRACT, image_digest="d"
    )
    assert evidence == {
        "schema": 1,
        "image_digest": "d",
        "demo": {"demo": True},
        "submodule": {"workspace": "submodule-leg"},
    }
    assert seen["metrics"] == METRICS and seen["project"] == (tmp_path / "p").resolve()
    assert seen["workspace"].name == "demo-leg" and not seen["workspace"].exists()


def _cli(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--project",
        str(tmp_path),
        "--project-state",
        str(tmp_path),
        "--contract",
        str(CONTRACT),
        "--evidence",
        str(tmp_path / "out" / "measurement.json"),
        *extra,
    ]


def test_main_writes_the_evidence_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_trip(), "round_trip", lambda **kwargs: {"digest": kwargs["image_digest"]})
    assert _trip().main(_cli(tmp_path, "--image-digest", "sha256:x")) == 0
    written = tmp_path / "out" / "measurement.json"
    assert json.loads(written.read_text()) == {"digest": "sha256:x"}
    assert [path.name for path in written.parent.iterdir()] == ["measurement.json"]
    assert "passed" in capsys.readouterr().out


def test_main_fails_without_writing_evidence_when_the_round_trip_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def fail(**_kwargs: Any) -> dict[str, Any]:
        raise _trip().RoundTripError("demo: booley measure exited 1")

    monkeypatch.setattr(_trip(), "round_trip", fail)
    assert _trip().main(_cli(tmp_path, "--image-digest", "sha256:x")) == 1
    assert "exited 1" in capsys.readouterr().err
    assert not (tmp_path / "out").exists()


def test_main_refuses_a_blank_image_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_trip(), "round_trip", lambda **_kwargs: pytest.fail("ran"))
    assert _trip().main(_cli(tmp_path, "--image-digest", " ")) == 2
    assert "must not be blank" in capsys.readouterr().err


def test_git_failure_names_the_command(tmp_path: Path) -> None:
    with pytest.raises(_trip().RoundTripError, match="git rev-parse HEAD failed"):
        _trip()._git(tmp_path, "rev-parse", "HEAD")


def test_measure_runs_booley_measure_in_the_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(command=command, **kwargs)
        return _completed(0)

    monkeypatch.setattr(subprocess, "run", run)
    _trip()._measure(tmp_path, tmp_path / ".booley", "--json")
    assert seen["command"][1:] == ["-m", "booley.harness.booley", "measure", "--json"]
    assert seen["cwd"] == tmp_path
    assert seen["env"]["BOOLEY_DIR"] == str(tmp_path / ".booley")
    assert seen["env"]["NO_COLOR"] == "1"
