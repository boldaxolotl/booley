"""`booley coverage uncovered`: list the uncovered points of one retained coverage record."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from booley.flows.sim.coverage_campaign import DurableTargetIdentity, encode_coverage_point_id
from booley.flows.sim.coverage_campaign_store import publish_coverage_campaign
from booley.flows.sim.coverage_provenance import coverage_digest
from booley.harness import booley as cli
from tests.flows.sim.coverage_campaign_test_support import decode_document
from tests.flows.sim.test_coverage_campaign import _valid_document

pytestmark = pytest.mark.usefixtures("host_command")

_HINT = "Pass sim/<N>/targets/<target>/coverage.json of a retained coverage run."


@pytest.fixture
def host_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the CLI as a host terminal, where no Sandbox or Project is present."""
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)


def _point(
    source: str,
    line: int,
    metric: str,
    subject: dict[str, object],
    *,
    hits: dict[str, int] | None = None,
    disposition: dict[str, object] | None = None,
) -> dict[str, object]:
    identity = {
        "metric": metric,
        "location": {
            "source": source,
            "start": {"line": line, "column": 3},
            "end": {"line": line, "column": 28},
        },
        "hierarchy": "TOP.counter",
        "subject": subject,
        "collector": {"record_type": f"v_{metric}", "native_key": f"{line}:{metric}"},
    }
    return {
        "id": encode_coverage_point_id(identity),
        "identity": identity,
        "hits_by_run": hits or {},
        "disposition": disposition or {"kind": "eligible"},
    }


def _waiver(waiver_id: str) -> dict[str, object]:
    return {
        "kind": "waived",
        "reason": "unreachable",
        "waiver_id": waiver_id,
        "waiver_file": "coverage/waivers.toml",
        "waiver_fingerprint": "sha256:" + "8" * 64,
        "provenance": {
            "justification": "Reset prevents this state",
            "approved_by": "reviewer",
            "approved_at": "2026-09-10T08:00:00Z",
            "approval_ref": "review:42",
            "proof": {"kind": "formal", "reference": "proof:counter"},
        },
    }


def _mixed_points() -> list[dict[str, object]]:
    """Out-of-order uncovered points in two sources, plus ones that must not be listed."""
    hit = {"run:reset": 2}
    return [
        _point("rtl/b.sv", 5, "line", {"basic_block": 3}),
        _point("rtl/a.sv", 20, "line", {"basic_block": 1}),
        _point("rtl/a.sv", 7, "line", {"basic_block": 0}),
        _point("rtl/a.sv", 7, "branch", {"outcome": "else"}),
        _point("rtl/a.sv", 30, "toggle", {"bit": "wr_ptr[2]:1->0"}),
        _point("rtl/b.sv", 1, "branch", {"outcome": "if"}),
        _point("rtl/b.sv", 6, "line", {"basic_block": ""}),
        _point("rtl/a.sv", 2, "line", {"basic_block": 9}, hits=hit),
        _point("rtl/a.sv", 31, "line", {"basic_block": 4}, disposition=_waiver("waiver:a")),
        _point("rtl/b.sv", 9, "branch", {"outcome": "if"}, disposition=_waiver("waiver:b")),
        _point("tb/tb.sv", 4, "line", {"basic_block": 2}, disposition={"kind": "unscored"}),
    ]


def _bind_closure(document: dict) -> None:
    """List every scored source, and the testbench, with digests that match their paths."""
    document["source_closure"] = {
        "rtl": [{"path": f"rtl/{name}.sv", "sha256": "sha256:" + "6" * 64} for name in "ab"],
        "testbench": [{"path": "tb/tb.sv", "sha256": "sha256:" + "7" * 64}],
    }
    for category in ("rtl", "testbench"):
        document["fingerprints"][category + "_sources"] = coverage_digest(
            document["source_closure"][category]
        )
    for metric in ("branch", "toggle"):
        document["collector"]["capabilities"].append(
            {"record_class": metric, "status": "reported"}
        )


def persist_record(
    root: Path, points: list[dict[str, object]] | None = None, *, status: str = "complete"
) -> Path:
    """Publish a Target-level record under ``root/reports/sim/12`` and return its path."""
    document = _valid_document()
    _bind_closure(document)
    document["points"] = _mixed_points() if points is None else points
    if status != "complete":
        document["collection"]["status"] = status
        document["normalization"]["status"] = "partial"
        document["rollups"] = []
    campaign = decode_document(document, DurableTargetIdentity(document["target"]["identity"]))
    target_dir = root / "reports/sim/12/targets/sim_counter"
    path = publish_coverage_campaign(target_dir, campaign).campaign
    (target_dir / "simulation.json").write_text(
        json.dumps(
            {
                "flow": "sim",
                "complete": True,
                "target": "sim_counter",
                "target_identity": document["target"]["identity"],
                "collection": status,
                "evaluation": "not_requested",
                "coverage_campaign": "coverage.json",
            }
        )
    )
    return path


def _run(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    return _booley(monkeypatch, capsys, "coverage", "uncovered", *argv)


def _booley(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], *argv: str
) -> tuple[int, str, str]:
    """Run ``booley <argv>``; an argparse exit is returned as its code, not raised."""
    monkeypatch.setattr(sys, "argv", ["booley", *argv])
    try:
        code = cli.main()
    except SystemExit as stop:
        code = int(stop.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


_ALL_LINES = [
    "rtl/a.sv:7  branch  else  TOP.counter",
    "rtl/a.sv:7  line  0  TOP.counter",
    "rtl/a.sv:20  line  1  TOP.counter",
    "rtl/a.sv:30  toggle  wr_ptr[2]:1->0  TOP.counter",
    "rtl/b.sv:1  branch  if  TOP.counter",
    "rtl/b.sv:5  line  3  TOP.counter",
    "rtl/b.sv:6  line  -  TOP.counter",
]


def test_uncovered_prints_one_line_per_point_and_a_summary(tmp_path, monkeypatch, capsys) -> None:
    path = persist_record(tmp_path)

    code, out, err = _run(monkeypatch, capsys, str(path))

    assert (code, err) == (0, "")
    assert out.splitlines() == [*_ALL_LINES, "7 uncovered points; 2 waived points not listed"]


def test_summary_reports_waived_points_left_out(tmp_path, monkeypatch, capsys) -> None:
    waived_only = [
        _point("rtl/a.sv", 1, "line", {"basic_block": 0}),
        _point("rtl/a.sv", 2, "line", {"basic_block": 1}, disposition=_waiver("waiver:one")),
    ]
    path = persist_record(tmp_path, waived_only)

    code, out, _err = _run(monkeypatch, capsys, str(path))

    assert code == 0
    assert out.splitlines() == [
        "rtl/a.sv:1  line  0  TOP.counter",
        "1 uncovered point; 1 waived point not listed",
    ]


def test_summary_has_no_waived_clause_without_waived_points(tmp_path, monkeypatch, capsys) -> None:
    path = persist_record(tmp_path, [_point("rtl/a.sv", 1, "line", {"basic_block": 0})])

    _code, out, _err = _run(monkeypatch, capsys, str(path))

    assert out.splitlines()[-1] == "1 uncovered point"


@pytest.mark.parametrize(
    ("filters", "lines", "summary"),
    [
        (["--metric", "line"], [1, 2, 5, 6], "4 uncovered points; 1 waived point not listed"),
        (["--source", "rtl/b.sv"], [4, 5, 6], "3 uncovered points; 1 waived point not listed"),
        (
            ["--metric", "branch", "--source", "rtl/a.sv"],
            [0],
            "1 uncovered point",
        ),
        (
            ["--metric", "branch", "--source", "rtl/b.sv"],
            [4],
            "1 uncovered point; 1 waived point not listed",
        ),
        (["--metric", "Branch"], [], "0 uncovered points"),
        (["--source", "rtl/b.s"], [], "0 uncovered points"),
        (["--metric", "line", "--source", "tb/tb.sv"], [], "0 uncovered points"),
        (["--metric", "nonsense"], [], "0 uncovered points"),
    ],
    ids=["metric", "source", "both", "both-waived", "case", "prefix", "unscored", "unknown"],
)
def test_filters_combine_and_unmatched_filter_prints_zero_count(
    tmp_path, monkeypatch, capsys, filters, lines, summary
) -> None:
    path = persist_record(tmp_path)
    everything = [*_ALL_LINES]

    code, out, _err = _run(monkeypatch, capsys, str(path), *filters)

    assert code == 0
    assert out.splitlines() == [everything[index] for index in lines] + [summary]


def test_fully_covered_record_prints_zero_count_and_exits_0(tmp_path, monkeypatch, capsys) -> None:
    path = persist_record(
        tmp_path, [_point("rtl/a.sv", 1, "line", {"basic_block": 0}, hits={"run:reset": 1})]
    )

    code, out, err = _run(monkeypatch, capsys, str(path))

    assert (code, out, err) == (0, "0 uncovered points\n", "")


def _remove_point_store(path: Path) -> None:
    store = path.parent / json.loads(path.read_text())["point_store"]["path"]
    store.unlink()


def _corrupt_point_store(path: Path) -> None:
    store = path.parent / json.loads(path.read_text())["point_store"]["path"]
    raw = bytearray(store.read_bytes())
    raw[len(raw) // 2] ^= 0xFF
    store.write_bytes(bytes(raw))


def _not_json(path: Path) -> None:
    path.write_text("not json {", encoding="utf-8")


def _not_a_campaign(path: Path) -> None:
    path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")


def _nested_attempt_manifest(path: Path) -> Path:
    """Copy the record to where an attempt writes its own manifest."""
    nested = path.parents[1] / "record/work-items/w1/attempts/a1/coverage-record"
    nested.mkdir(parents=True)
    for item in path.parent.iterdir():
        if item.is_file():
            (nested / item.name).write_bytes(item.read_bytes())
    return nested / "coverage.json"


@pytest.mark.parametrize(
    "defect",
    [
        "missing-file",
        "not-json",
        "not-a-campaign",
        "wrong-file-name",
        "nested-attempt-manifest",
        "point-store-missing",
        "point-store-corrupt",
    ],
)
def test_unreadable_record_exits_2_naming_the_file(
    tmp_path, monkeypatch, capsys, defect: str
) -> None:
    path = persist_record(tmp_path)
    argument = path
    if defect == "missing-file":
        argument = path.parent / "absent" / "coverage.json"
    elif defect == "not-json":
        _not_json(path)
    elif defect == "not-a-campaign":
        _not_a_campaign(path)
    elif defect == "wrong-file-name":
        argument = path.rename(path.with_name("campaign.json"))
    elif defect == "nested-attempt-manifest":
        argument = _nested_attempt_manifest(path)
    elif defect == "point-store-missing":
        _remove_point_store(path)
    else:
        _corrupt_point_store(path)

    code, out, err = _run(monkeypatch, capsys, str(argument))

    assert code == 2
    assert out == ""
    assert err.startswith(f"ERROR: cannot read coverage record {argument}: ")
    assert _HINT in err.splitlines()[-1]
    assert "Traceback" not in err


def test_relative_path_with_parent_segments_is_read(tmp_path, monkeypatch, capsys) -> None:
    persist_record(tmp_path)
    (tmp_path / "rtl").mkdir()
    monkeypatch.chdir(tmp_path / "rtl")

    code, out, err = _run(
        monkeypatch, capsys, "../reports/sim/12/targets/sim_counter/coverage.json"
    )

    assert (code, err) == (0, "")
    assert out.splitlines()[:2] == _ALL_LINES[:2]


@pytest.mark.parametrize("status", ["collector_error", "incomplete", "incompatible"])
def test_collector_error_record_warns_and_exits_0(
    tmp_path, monkeypatch, capsys, status: str
) -> None:
    path = persist_record(tmp_path, points=[], status=status)

    code, out, err = _run(monkeypatch, capsys, str(path))

    assert code == 0
    assert out == "0 uncovered points\n"
    assert f"WARNING: coverage collection is {status.upper()}" in err
    assert "not a coverage result" in err


def test_incompatible_native_format_warns_even_when_collection_is_complete(
    tmp_path, monkeypatch, capsys
) -> None:
    path = persist_record(tmp_path)
    manifest = json.loads(path.read_text())
    manifest["collector"]["native_format"]["compatibility"] = "incompatible"
    path.write_text(json.dumps(manifest))

    code, out, err = _run(monkeypatch, capsys, str(path))

    assert code == 0
    assert out.splitlines()[:2] == _ALL_LINES[:2]
    assert "WARNING: coverage collection is INCOMPATIBLE" in err


def test_complete_record_prints_no_warning(tmp_path, monkeypatch, capsys) -> None:
    path = persist_record(tmp_path)

    _code, _out, err = _run(monkeypatch, capsys, str(path))

    assert err == ""


@pytest.mark.parametrize("inside", [False, True], ids=["host", "sandbox"])
def test_command_reads_the_record_on_host_and_in_sandbox(
    tmp_path, monkeypatch, capsys, inside: bool
) -> None:
    path = persist_record(tmp_path)
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: inside)

    code, out, err = _run(monkeypatch, capsys, str(path))

    assert (code, err) == (0, "")
    assert out.splitlines() == [*_ALL_LINES, "7 uncovered points; 2 waived points not listed"]


def test_command_needs_no_project_and_refuses_project_selection(
    tmp_path, monkeypatch, capsys
) -> None:
    path = persist_record(tmp_path)
    monkeypatch.chdir(tmp_path)  # no booley.toml anywhere above

    code, _out, err = _run(monkeypatch, capsys, str(path))
    assert (code, err) == (0, "")

    refused, _out, refused_err = _run(monkeypatch, capsys, str(path), "--project", "x")
    assert refused == 2
    assert "--project" in refused_err


def test_missing_subcommand_is_a_usage_error(monkeypatch, capsys) -> None:
    code, _out, err = _booley(monkeypatch, capsys, "coverage")

    assert code == 2
    assert "required" in err


def test_command_is_registered_with_help_text(monkeypatch, capsys) -> None:
    code, out, _err = _run(monkeypatch, capsys, "--help")

    assert code == 0
    for fragment in ("COVERAGE_JSON", "--metric", "--source", "sim/<N>/targets/<target>"):
        assert fragment in out
