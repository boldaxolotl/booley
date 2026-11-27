"""The core resolution owner grades Sandbox ``.core`` resolution without Doctor.

Every assertion reads the returned ``CoreResolutionResult``: severity, message,
fix, always-shown details and the selectors that produced a verdict. The owner
module is imported inside a helper so this file collects, and each test fails on
its own, before the owner exists. ``.core`` files are real and ``TargetCatalog``
is real; the Sandbox command builder is a test function whose command is a real
child that replays a canned verdict.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest
from tests.canned_process_support import canned_command

from booley.audit.diagnostic_results import Severity
from booley.flows.base import SubprocessResult
from booley.runtime import session_runtime

_CORE = """\
CAPI=2:
name: ::{name}:0
targets:
{targets}"""


def _owner():
    """The core resolution owner module, imported where used."""
    return importlib.import_module("booley.runtime.core_resolution")


def _write_core(
    root: Path, name: str, targets: tuple[str, ...], depend: tuple[str, ...] = ()
) -> None:
    """Write ``<name>.core`` declaring each Target as a Verilator sim Target."""
    extra = f", depend: [{', '.join(repr(d) for d in depend)}]" if depend else ""
    body = "".join(
        f"  {t}: {{flow: sim, flow_options: {{tool: verilator}}{extra}}}\n" for t in targets
    )
    (root / f"{name}.core").write_bytes(
        _CORE.format(name=name, targets=body).encode("utf-8").replace(b"\r\n", b"\n")
    )


class _Sandbox:
    """A Sandbox command builder: records each argv and answers with a real child.

    *reply* maps the dispatched payload to the child's exit code and stdout. The
    child fails unless the owner starts it in the Project Root *root*.
    """

    def __init__(self, root: Path, reply):
        self.root = root
        self.reply = reply
        self.built: list[list[str]] = []

    def command(self, inner: list[str]) -> list[str]:
        self.built.append(inner)
        code, stdout = self.reply(json.loads(inner[-1]))
        return canned_command(SubprocessResult(returncode=code, stdout=stdout), cwd=self.root)


def _verdicts(out) -> tuple[int, str]:
    return 0, "[[CORE_RESOLVE_JSON]]" + json.dumps(out)


def _ok_for_all(payload):
    return _verdicts([{"selector": item["selector"], "ok": True} for item in payload])


def _inspect(root: Path, selectors, sandbox, *, available=True):
    owner = _owner()
    return owner.inspect_core_resolution(
        owner.CoreResolutionRequest(root, tuple(selectors), available, sandbox.command)
    )


def _rows(result):
    return [(f.severity, f.message, f.fix) for f in result.report.findings]


def test_resolving_target_passes_and_sends_canonical_payload(tmp_path):
    """Two cores sharing a Target name resolve; only the selected Targets are sent."""
    _write_core(tmp_path, "first", ("smoke", "unselected"))
    _write_core(tmp_path, "second", ("smoke",))
    sandbox = _Sandbox(tmp_path, _ok_for_all)

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


def test_failing_target_fails_with_an_always_shown_excerpt(tmp_path):
    """A failing Target FAILs with its fix and the error excerpt; the other still passes."""
    _write_core(tmp_path, "unit", ("sim_ok", "sim_bad"))
    err = "Parse error. Ignoring file unit.core:\nfilesets.tb.depend must be array\nlast line"

    def reply(payload):
        return _verdicts(
            [
                {"selector": p["selector"], "ok": True}
                if p["selector"] == "sim_ok"
                else {"selector": p["selector"], "ok": False, "err": err}
                for p in payload
            ]
        )

    result = _inspect(tmp_path, ("sim_ok", "sim_bad"), _Sandbox(tmp_path, reply))

    assert _rows(result) == [
        (
            Severity.FAIL,
            ".core Target 'sim_bad' fails to resolve",
            "fix unit.core (declares Target 'sim_bad') or its depends graph",
        ),
        (Severity.PASS, ".core Target 'sim_ok' resolves", ""),
    ]
    assert [(d.after_finding, d.message, d.verbose_only) for d in result.report.details] == [
        (1, "    Parse error. Ignoring file unit.core:", False),
        (1, "    filesets.tb.depend must be array", False),
        (1, "    last line", False),
    ]
    assert result.completed == ("sim_bad", "sim_ok")


_FUSESOC_EXCERPT = "ERROR: Could not find core\nvendor_lib_name == 1.0-0"


def _failing_with(err: str):
    def reply(payload):
        return _verdicts([{"selector": p["selector"], "ok": False, "err": err} for p in payload])

    return reply


def _unprovided_hint(dependency: str) -> str:
    return (
        f"no Project .core provides '{dependency}': the Sandbox has no FuseSoC "
        "libraries and cannot fetch them; copy it into the repository as a .core, or drop "
        "the depend: (check first whether the testbench guards its use with an `ifdef` "
        "or defines the module itself)"
    )


_GENERIC_FIX = "fix unit.core (declares Target 'sim') or its depends graph"


@pytest.mark.parametrize("case", ["unprovided", "transitive", "provided", "edited"])
def test_failed_target_fix_names_unprovided_dependency_or_core(tmp_path, case):
    """The fix names a ``depend:`` no Project core provides, else the failing ``.core``."""
    depend = {
        "unprovided": ">=::vlog_tb_utils:1.1",
        "transitive": "::mid:0",
        "provided": "::mid:0",
        "edited": "::missing:0",
    }[case]
    _write_core(tmp_path, "unit", ("sim",), depend=(depend,))
    if case == "transitive":
        # Only the dependency no Project core provides is named, through a Project core.
        (tmp_path / "mid.core").write_bytes(
            b"CAPI=2:\nname: ::mid:0\nfilesets:\n  rtl:\n    depend: ['::wiredelay:0']\n"
        )
    elif case == "provided":
        (tmp_path / "mid.core").write_bytes(b"CAPI=2:\nname: ::mid:0\n")
    fix = {
        "unprovided": _unprovided_hint(">=::vlog_tb_utils:1.1"),
        "transitive": _unprovided_hint("::wiredelay:0"),
    }.get(case, _GENERIC_FIX)
    err = "ERROR: Could not find core\nvendor_lib_name == 1.0-0"
    if case == "unprovided":
        err = f"{err}\n{fix}"  # the Sandbox message ends with the hint; the excerpt drops it

    def reply(payload):
        if case == "edited":  # a .core edited while the Sandbox runs must not abort the report
            (tmp_path / "unit.core").write_bytes(b"CAPI=2:\nname: ::unit:0\n# edited\n")
        return _failing_with(err)(payload)

    result = _inspect(tmp_path, ("sim",), _Sandbox(tmp_path, reply))

    assert _rows(result) == [(Severity.FAIL, ".core Target 'sim' fails to resolve", fix)]
    assert [d.message for d in result.report.details] == [
        "    ERROR: Could not find core",
        "    vendor_lib_name == 1.0-0",
    ]


def test_unselectable_target_fails_before_dispatch(tmp_path):
    """A selector the catalog rejects FAILs with its reason and is never dispatched."""
    _write_core(tmp_path, "unit", ("sim_ok",))
    sandbox = _Sandbox(tmp_path, _ok_for_all)

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

    alone = _Sandbox(tmp_path, _ok_for_all)
    only = _inspect(tmp_path, ("removed_target",), alone)
    assert [row[:2] for row in _rows(only)] == [
        (Severity.FAIL, ".core Target 'removed_target' fails to resolve")
    ]
    assert alone.built == []
    assert only.completed == ()


def test_missing_incomplete_or_duplicate_verdicts_fail_closed(tmp_path):
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
        sandbox = _Sandbox(tmp_path, lambda _payload, reply=(code, stdout): reply)
        result = _inspect(tmp_path, ("sim_ok", "sim_bad"), sandbox)
        assert _rows(result)[-1] == (
            Severity.FAIL,
            ".core resolvability produced incomplete Sandbox evidence",
            "check the Sandbox / FuseSoC",
        ), label
        assert result.completed == (), label


@pytest.mark.parametrize(
    ("failure", "cause"),
    [("issuance", "bad issuance"), ("missing executable", ""), ("timeout", "timed out")],
)
def test_sandbox_start_failure_fails(tmp_path, failure, cause):
    """A Sandbox that cannot start, spawn or finish is one FAIL naming the cause."""
    _write_core(tmp_path, "unit", ("sim_ok",))
    sandbox = _Sandbox(tmp_path, _ok_for_all)

    def command(_inner):
        if failure == "issuance":
            raise session_runtime.SessionError("bad issuance")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(["sandbox"], 600)
        return [str(tmp_path / "no-sandbox")]

    sandbox.command = command

    result = _inspect(tmp_path, ("sim_ok",), sandbox)

    [(severity, message, fix)] = _rows(result)
    assert (severity, fix) == (Severity.FAIL, "check the Sandbox")
    assert message.startswith(".core resolvability could not complete in the Sandbox: ")
    assert cause in message
    assert result.completed == ()


def test_pending_recovery_skips(tmp_path):
    _write_core(tmp_path, "unit", ("sim_ok",))
    sandbox = _Sandbox(tmp_path, _ok_for_all)
    sandbox.command = lambda _inner: (_ for _ in ()).throw(session_runtime.RecoveryPendingError())
    result = _inspect(tmp_path, ("sim_ok",), sandbox)
    assert [f.severity for f in result.report.findings] == [Severity.SKIP]
    assert result.report.findings[0].message.endswith("recovery is pending on this host")


def test_unavailable_runtime_skips(tmp_path):
    """With no Sandbox runtime the owner SKIPs and touches neither catalog nor Sandbox."""
    sandbox = _Sandbox(tmp_path, _ok_for_all)

    result = _inspect(tmp_path, ("sim_ok",), sandbox, available=False)

    assert _rows(result) == [
        (Severity.SKIP, ".core resolvability skipped - Sandbox runtime unavailable", "")
    ]
    assert sandbox.built == []
    assert result.completed == ()
