"""The core resolution owner grades Sandbox ``.core`` resolution without Doctor.

Every assertion reads the returned ``CoreResolutionResult``: severity, message,
fix, always-shown details and the selectors that produced a verdict. The owner
module is imported inside a helper so this file collects, and each test fails on
its own, before the owner exists. ``.core`` files are real and ``TargetCatalog``
is real; the Sandbox command builder is a test function and ``subprocess.run`` is
replaced on the owner module's own binding.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from booley.audit.diagnostic_results import Severity
from booley.runtime import session_runtime

_CORE = """\
CAPI=2:
name: ::{name}:0
targets:
{targets}"""


def _owner():
    """The core resolution owner module, imported where used."""
    return importlib.import_module("booley.runtime.core_resolution")


def _write_core(root: Path, name: str, targets: tuple[str, ...]) -> None:
    """Write ``<name>.core`` declaring each Target as a Verilator sim Target."""
    body = "".join(f"  {t}: {{flow: sim, flow_options: {{tool: verilator}}}}\n" for t in targets)
    (root / f"{name}.core").write_bytes(
        _CORE.format(name=name, targets=body).encode("utf-8").replace(b"\r\n", b"\n")
    )


class _Sandbox:
    """A fake Sandbox: records the argv it is asked to build and each run."""

    def __init__(self, monkeypatch, verdicts):
        self.built: list[list[str]] = []
        self.runs: list[tuple[list[str], dict]] = []
        self._verdicts = verdicts
        monkeypatch.setattr(
            _owner(),
            "subprocess",
            SimpleNamespace(run=self._run, SubprocessError=subprocess.SubprocessError),
        )

    def command(self, inner: list[str]) -> list[str]:
        self.built.append(inner)
        return ["sandbox", *inner]

    def _run(self, argv, **kwargs):
        self.runs.append((argv, kwargs))
        payload = json.loads(argv[-1])
        return self._verdicts(argv, payload)


def _ok_for_all(argv, payload):
    out = [{"selector": item["selector"], "ok": True} for item in payload]
    return subprocess.CompletedProcess(argv, 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(out), "")


def _inspect(root: Path, selectors, sandbox, *, available=True):
    owner = _owner()
    return owner.inspect_core_resolution(
        owner.CoreResolutionRequest(root, tuple(selectors), available, sandbox.command)
    )


def _rows(result):
    return [(f.severity, f.message, f.fix) for f in result.report.findings]


def test_resolving_target_passes_and_sends_canonical_payload(tmp_path, monkeypatch):
    """Two cores sharing a Target name resolve; only the selected Targets are sent."""
    _write_core(tmp_path, "first", ("smoke", "unselected"))
    _write_core(tmp_path, "second", ("smoke",))
    sandbox = _Sandbox(monkeypatch, _ok_for_all)

    result = _inspect(tmp_path, ("second#smoke", "first#smoke"), sandbox)

    assert _rows(result) == [
        (Severity.PASS, ".core Target 'first#smoke' resolves", ""),
        (Severity.PASS, ".core Target 'second#smoke' resolves", ""),
    ]
    assert result.completed == ("first#smoke", "second#smoke")
    expected = [
        {
            "selector": "first#smoke",
            "name": "smoke",
            "vlnv": "::first:0",
            "build_key": "7c673222a152d616",
        },
        {
            "selector": "second#smoke",
            "name": "smoke",
            "vlnv": "::second:0",
            "build_key": "a33eba0162e0e258",
        },
    ]
    assert sandbox.built[0][3] == json.dumps(expected, separators=(",", ":"))
    assert sandbox.built[0][:2] == ["python3", "-c"]
    assert "[[CORE_RESOLVE_JSON]]" in sandbox.built[0][2]
    argv, kwargs = sandbox.runs[0]
    assert argv == ["sandbox", *sandbox.built[0]]
    assert kwargs["cwd"] == tmp_path
    assert kwargs["timeout"] == 600


def test_failing_target_fails_with_an_always_shown_excerpt(tmp_path, monkeypatch):
    """A failing Target FAILs with its fix and the error excerpt; the other still passes."""
    _write_core(tmp_path, "unit", ("sim_ok", "sim_bad"))
    err = "Parse error. Ignoring file unit.core:\nfilesets.tb.depend must be array\nlast line"

    def verdicts(argv, payload):
        out = [
            {"selector": p["selector"], "ok": True}
            if p["selector"] == "sim_ok"
            else {"selector": p["selector"], "ok": False, "err": err}
            for p in payload
        ]
        return subprocess.CompletedProcess(argv, 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(out), "")

    result = _inspect(tmp_path, ("sim_ok", "sim_bad"), _Sandbox(monkeypatch, verdicts))

    assert _rows(result) == [
        (
            Severity.FAIL,
            ".core Target 'sim_bad' fails to resolve",
            "fix the .core / depends graph",
        ),
        (Severity.PASS, ".core Target 'sim_ok' resolves", ""),
    ]
    assert [(d.after_finding, d.message, d.verbose_only) for d in result.report.details] == [
        (1, "    Parse error. Ignoring file unit.core:", False),
        (1, "    filesets.tb.depend must be array", False),
        (1, "    last line", False),
    ]
    assert result.completed == ("sim_bad", "sim_ok")


def test_unselectable_target_fails_before_dispatch(tmp_path, monkeypatch):
    """A selector the catalog rejects FAILs with its reason and is never dispatched."""
    _write_core(tmp_path, "unit", ("sim_ok",))
    sandbox = _Sandbox(monkeypatch, _ok_for_all)

    result = _inspect(tmp_path, ("removed_target", "sim_ok"), sandbox)

    rows = _rows(result)
    assert rows[0] == (
        Severity.FAIL,
        ".core Target 'removed_target' fails to resolve",
        "fix the .core / depends graph",
    )
    assert rows[1] == (Severity.PASS, ".core Target 'sim_ok' resolves", "")
    assert result.report.details, "the selection error is shown as an excerpt"
    assert all(not d.verbose_only and d.after_finding == 1 for d in result.report.details)
    assert [item["selector"] for item in json.loads(sandbox.built[0][3])] == ["sim_ok"]
    assert result.completed == ("sim_ok",)

    alone = _Sandbox(monkeypatch, _ok_for_all)
    only = _inspect(tmp_path, ("removed_target",), alone)
    assert [row[:2] for row in _rows(only)] == [
        (Severity.FAIL, ".core Target 'removed_target' fails to resolve")
    ]
    assert alone.built == [] and alone.runs == []
    assert only.completed == ()


def test_missing_incomplete_or_duplicate_verdicts_fail_closed(tmp_path, monkeypatch):
    """No bounded verdict per selected Target means incomplete evidence and no completion."""
    _write_core(tmp_path, "unit", ("sim_ok", "sim_bad"))
    marker = "[[CORE_RESOLVE_JSON]]"
    good = {"selector": "sim_ok", "ok": True}
    other = {"selector": "sim_bad", "ok": True}
    cases = {
        "no marker line": (0, "no verdict here\n"),
        "non-zero exit": (1, marker + json.dumps([good, other])),
        "empty list": (0, marker + "[]"),
        "too short": (0, marker + json.dumps([good])),
        "unknown selector": (0, marker + json.dumps([good, {"selector": "x", "ok": True}])),
        "duplicate selector": (0, marker + json.dumps([good, good])),
        "non-bool ok": (0, marker + json.dumps([good, {"selector": "sim_bad", "ok": "yes"}])),
        "not an object": (0, marker + json.dumps([good, 7])),
        "not a list": (0, marker + "{}"),
        "malformed json": (0, marker + "[{"),
    }
    for label, (code, stdout) in cases.items():
        sandbox = _Sandbox(
            monkeypatch,
            lambda argv, _payload, code=code, stdout=stdout: subprocess.CompletedProcess(
                argv, code, stdout, ""
            ),
        )
        result = _inspect(tmp_path, ("sim_ok", "sim_bad"), sandbox)
        assert _rows(result)[-1] == (
            Severity.FAIL,
            ".core resolvability produced incomplete Sandbox evidence",
            "check the Sandbox / FuseSoC",
        ), label
        assert result.completed == (), label


def test_sandbox_start_failure_fails(tmp_path, monkeypatch):
    """A Sandbox that cannot start, spawn or finish is one FAIL naming the cause."""
    _write_core(tmp_path, "unit", ("sim_ok",))
    timeout = subprocess.TimeoutExpired(["sandbox"], 600)
    failures = (
        session_runtime.SessionError("bad issuance"),
        OSError("no such executable"),
        timeout,
    )
    for failure in failures:
        sandbox = _Sandbox(monkeypatch, lambda *_args: None)
        if isinstance(failure, session_runtime.SessionError):

            def refuse(_inner, failure=failure):
                raise failure

            sandbox.command = refuse
        else:

            def broken(_argv, _payload, failure=failure):
                raise failure

            sandbox._verdicts = broken
        result = _inspect(tmp_path, ("sim_ok",), sandbox)
        assert _rows(result) == [
            (
                Severity.FAIL,
                f".core resolvability could not complete in the Sandbox: {failure}",
                "check the Sandbox",
            )
        ], failure
        assert result.completed == (), failure


def test_unavailable_runtime_skips(tmp_path, monkeypatch):
    """With no Sandbox runtime the owner SKIPs and touches neither catalog nor Sandbox."""
    sandbox = _Sandbox(monkeypatch, _ok_for_all)

    result = _inspect(tmp_path, ("sim_ok",), sandbox, available=False)

    assert _rows(result) == [
        (Severity.SKIP, ".core resolvability skipped - Sandbox runtime unavailable", "")
    ]
    assert sandbox.built == [] and sandbox.runs == []
    assert result.completed == ()
