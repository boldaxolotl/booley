"""Durable process accounting and resolved EDA identity in endpoint reports."""

from __future__ import annotations

import builtins
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.flows import endpoint_reporting
from booley.runtime.endpoint_execution import EndpointOutcome
from tests.mcp_tools.test_base import ConcreteMcpTool


def fake_accounting(
    monkeypatch: pytest.MonkeyPatch, *, platform: str = "linux", peak: int = 32768
) -> list[int]:
    calls = []

    def getrusage(who):
        calls.append(who)
        return SimpleNamespace(
            ru_utime=1.25 if who == 0 else 2.5,
            ru_stime=0.25 if who == 0 else 0.5,
            ru_maxrss=peak if who == 0 else peak * 8,
        )

    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(
            RUSAGE_SELF=0,
            RUSAGE_CHILDREN=-1,
            getrusage=getrusage,
        ),
    )
    monkeypatch.setattr(
        endpoint_reporting, "sys", SimpleNamespace(**(vars(sys) | {"platform": platform}))
    )
    return calls


def report_endpoint(tmp_path: Path, *, target: str = "sim") -> ConcreteMcpTool:
    endpoint = ConcreteMcpTool()
    endpoint.parse_args(["--report-dir", str(tmp_path), "--target", target])
    return endpoint


@pytest.mark.parametrize(
    ("platform", "peak"), [("linux", 32768), ("darwin", 33554432), ("linux", 0)]
)
def test_report_resources_sum_cpu_and_take_largest_process_rss(
    tmp_path, monkeypatch, platform, peak
):
    host_platform = sys.platform
    fake_accounting(monkeypatch, platform=platform, peak=peak)
    assert sys.platform == host_platform, "accounting fixture changed process-wide platform"
    path = report_endpoint(tmp_path).write_report(EndpointOutcome())
    report = json.loads(path.read_text())
    expected_peak = 0 if peak == 0 else 256
    assert report.get("resources") == {
        "cpu_user_s": 3.75,
        "cpu_sys_s": 0.75,
        "peak_rss_mb": expected_peak,
    }


def test_resource_import_unavailable_omits_resources(tmp_path, monkeypatch):
    original = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name == "resource":
            raise ImportError("unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    path = report_endpoint(tmp_path).write_report(EndpointOutcome())
    assert "resources" not in json.loads(path.read_text())


def test_accounting_error_preserves_report(tmp_path, monkeypatch, caplog):
    fake_accounting(monkeypatch)

    def failed(_who):
        raise OSError("accounting unavailable")

    monkeypatch.setattr(sys.modules["resource"], "getrusage", failed)
    path = report_endpoint(tmp_path).write_report(EndpointOutcome(report_text="verdict"))
    report = json.loads(path.read_text())
    assert "resources" not in report
    assert report["report_text"] == "verdict"
    assert "accounting unavailable" in caplog.text


def test_numbered_and_flat_reports_share_one_resource_snapshot(tmp_path, monkeypatch):
    calls = fake_accounting(monkeypatch)
    path = report_endpoint(tmp_path).write_report(EndpointOutcome())
    assert path.read_bytes() == (tmp_path / "test_endpoint.json").read_bytes()
    assert calls == [0, -1]


@pytest.mark.parametrize(
    ("targets", "known", "expected"),
    [
        (["sim"], {"sim": "Verilator"}, {"eda_tool": "Verilator"}),
        (["sim"], {}, {}),
        (
            ["sim", "other"],
            {"sim": "icarus", "other": "verilator"},
            {"eda_tools": {"sim": "icarus", "other": "verilator"}},
        ),
        (
            ["sim", "other"],
            {"sim": "icarus", "other": "icarus"},
            {"eda_tools": {"sim": "icarus", "other": "icarus"}},
        ),
        (
            ["core:vendor:lib:1#sim", "other"],
            {"other": "verilator"},
            {"eda_tools": {"core:vendor:lib:1#sim": None, "other": "verilator"}},
        ),
    ],
)
def test_report_tool_shape_tracks_selected_targets(tmp_path, targets, known, expected):
    endpoint = report_endpoint(tmp_path, target=",".join(targets))
    endpoint.reset_report_metadata()
    for target, tool in known.items():
        endpoint.record_eda_tool(target, tool)
    path = endpoint.write_report(EndpointOutcome())
    report = json.loads(path.read_text())
    assert {key: report[key] for key in ("eda_tool", "eda_tools") if key in report} == expected
