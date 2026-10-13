"""Protect Phase 4 bounded-parallel Simulation Campaign QA assets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TAXI = ROOT / "qa/missions/taxi/fixtures/simulation-campaign"
# Documents captured unchanged from one Taxi QA run; ``timeline.json`` is the operator's
# measurement of that run with its SlotStore samples thinned.
CAPTURED = ROOT / "tests/qa/fixtures/taxi_campaign/parallel"
UART = ROOT / "qa/missions/uart/fixtures/simulation-campaign"
CAMPAIGN_ID = "9904b376-4087-4d49-a9be-55c4b8e27da8"
OUTER = "539e1228e49a48de8b6f967edbe31ce1"
WORKLOAD = "sha256:" + "9" * 64
TESTS = ("slow-first", "slow-fail", "slow-last")
GRADES = ("pass", "fail", "pass")
ATTEMPTS = (
    "f4f0095d-af6c-4076-893e-6eb48a93df1f",
    "1c607269-6641-4ec2-9b60-13691c3c63dd",
    "046164a5-a8a8-46c9-8f96-cd6d10052e3b",
)
# The first lane runs on the outer Job's own slot, so it has no child claim.
CHILDREN = (None, "8571ca69e04c4c1cbe47e53ef1f541c9", "201201777b614ea6a45854b41e047143")
WORK_ITEMS = (
    "item:0000:1e5af83dabf589f3",
    "item:0001:3ac778310874645b",
    "item:0002:43571b2fd86b6262",
)
BOUNDS = ((4, 8), (5, 9), (6, 10))


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _rewrite(path: Path, mutate) -> None:
    document = json.loads(path.read_text())
    mutate(document)
    _write_json(path, document)


def _work_item_directory(campaign: Path, index: int) -> Path:
    return campaign / "work-items" / f"{index + 1:04d}-{WORK_ITEMS[index].rsplit(':', 1)[1]}"


def _attempt_directory(campaign: Path, index: int) -> Path:
    return _work_item_directory(campaign, index) / "attempts" / f"0001-{ATTEMPTS[index]}"


def _run_directory(index: int) -> str:
    work_item = _work_item_directory(Path(), index).name
    return f"/work/.booley-qa/campaign/{CAMPAIGN_ID}/{work_item}/0001-{ATTEMPTS[index]}"


def _write_campaign(campaign: Path) -> None:
    """Write one completed three-attempt Campaign in the layout Booley publishes."""
    _write_json(
        campaign / "manifest.json",
        {
            "$schema": "booley.simulation-campaign-manifest/v3",
            "campaign_id": CAMPAIGN_ID,
            "fingerprints": {"workload_sha256": WORKLOAD},
            "origin": {"execution_id": OUTER, "invocation_id": 12},
            "work_items": [
                {
                    "kind": "ordinary_hdl",
                    "ordinal": index,
                    "selection": {"kind": "named", "names": [name]},
                    "work_item_id": WORK_ITEMS[index],
                }
                for index, name in enumerate(TESTS)
            ],
        },
    )
    manifest_sha256 = _digest((campaign / "manifest.json").read_bytes().rstrip(b"\n"))
    common = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": manifest_sha256,
        "workload_sha256": WORKLOAD,
    }
    for index in range(len(TESTS)):
        _write_attempt(campaign, index, common)
    _write_json(
        campaign / "summary.json",
        {
            "$schema": "booley.simulation-campaign-summary/v1",
            **common,
            "aggregate_grade": "fail",
            "complete": True,
            "completed": list(WORK_ITEMS),
            "interrupted": [],
            "pending": [],
        },
    )


def _write_attempt(campaign: Path, index: int, common: dict[str, str]) -> None:
    directory = _attempt_directory(campaign, index)
    identity = {**common, "attempt_id": ATTEMPTS[index], "work_item_id": WORK_ITEMS[index]}
    log = f"CMD: snapshot/Vcampaign_tb +test={TESTS[index]}\n[SIM_RESULT] done\n".encode()
    (directory / "evidence").mkdir(parents=True)
    (directory / "evidence/0001-run.log").write_bytes(log)
    _write_json(
        directory / "attempt.json",
        {
            "$schema": "booley.simulation-attempt/v1",
            **identity,
            "attempt_ordinal": 1,
            "child_execution_id": CHILDREN[index],
            "run_directory": {
                "configured": ".booley-qa/campaign/{campaign}/{test}/{attempt}",
                "kind": "templated",
                "owned": True,
                "resolved": _run_directory(index),
            },
        },
    )
    _write_json(
        _work_item_directory(campaign, index) / "result.json",
        _result(index, identity, log),
    )


def _result(index: int, identity: dict[str, str], log: bytes) -> dict[str, object]:
    return {
        "$schema": "booley.simulation-result/v2",
        **identity,
        "attempt_ordinal": 1,
        "evidence": [
            {
                "bytes": len(log),
                "kind": "live_run_log",
                "owner": ATTEMPTS[index],
                "path": "evidence/0001-run.log",
                "sha256": _digest(log),
            }
        ],
        "grade": GRADES[index],
        "observations": [
            {
                "assertion_count": 0,
                "assertions": "clean",
                "cycle_count": None,
                "detail": {"reason": ""},
                "execution": "completed",
                "failure_class": None if GRADES[index] == "pass" else "design",
                "functional": GRADES[index],
                "test": TESTS[index],
            }
        ],
        "runtime_inputs": [],
        "state": "completed",
    }


def _sample(at_ns: int, holders: list[str], waiters: list[str]) -> dict[str, object]:
    return {"at_ns": at_ns, "heavy_holders": holders, "heavy_waiters": waiters}


def _write_timeline(timeline: Path) -> None:
    """Write the operator's measurement of the Campaign written by ``_write_campaign``."""
    first, second = CHILDREN[1], CHILDREN[2]
    _write_json(
        timeline,
        {
            "outer_execution_id": OUTER,
            "slot_samples": [
                _sample(0, [], []),
                _sample(1, [OUTER], []),
                _sample(2, [OUTER], [first]),
                _sample(3, [OUTER, first], [second]),
                _sample(4, [OUTER, first, second], []),
                _sample(7, [OUTER, first, second], []),
                _sample(9, [OUTER, second], []),
                _sample(10, [OUTER], []),
                _sample(11, [], []),
            ],
            "final_slot_state": {"heavy_holders": [], "heavy_waiters": []},
            "claim_transitions": [
                {"at_ns": 2, "child_execution_id": first, "state": "submitted"},
                {"at_ns": 3, "child_execution_id": first, "state": "promoted"},
                {"at_ns": 3, "child_execution_id": second, "state": "submitted"},
                {"at_ns": 4, "child_execution_id": second, "state": "promoted"},
                {"at_ns": 9, "child_execution_id": first, "state": "released"},
                {"at_ns": 10, "child_execution_id": second, "state": "released"},
            ],
            "intervals": [_interval(timeline.parent, index) for index in range(len(TESTS))],
        },
    )


def _interval(root: Path, index: int) -> dict[str, object]:
    relative = Path("outputs") / ATTEMPTS[index] / "qa-shared-name.txt"
    raw = (TESTS[index] + "\n").encode()
    (root / relative).parent.mkdir(parents=True, exist_ok=True)
    (root / relative).write_bytes(raw)
    start, end = BOUNDS[index]
    return {
        "test": TESTS[index],
        "start_ns": start,
        "end_ns": end,
        "run_directory": _run_directory(index),
        "relative_output": "qa-shared-name.txt",
        "token": TESTS[index],
        "output_text": TESTS[index],
        "output": {"path": relative.as_posix(), "sha256": _digest(raw)},
        "attempt_id": ATTEMPTS[index],
        "child_execution_id": CHILDREN[index],
        "work_item_id": WORK_ITEMS[index],
    }


@pytest.fixture
def evidence(tmp_path: Path) -> tuple[Path, Path]:
    campaign = tmp_path / "campaign"
    timeline = tmp_path / "timeline.json"
    _write_campaign(campaign)
    _write_timeline(timeline)
    return timeline, campaign


@pytest.fixture(scope="module")
def validator():
    return _module("taxi_parallel_validator", TAXI / "validate_parallel.py")


def test_taxi_parallel_validator_accepts_published_campaign_shape(validator, evidence) -> None:
    timeline, campaign = evidence
    assert validator.validate_heavy_cap(timeline, 3) == {"max_heavy": 3, "peak_simulators": 3}
    validator.validate_attempt_isolation(timeline, campaign)
    validator.validate_continue_after_failure(
        campaign / "manifest.json",
        campaign / "summary.json",
        [_work_item_directory(campaign, index) / "result.json" for index in range(3)],
    )


def test_taxi_heavy_cap_accepts_captured_run(validator) -> None:
    assert validator.validate_heavy_cap(CAPTURED / "timeline.json", 3) == {
        "max_heavy": 3,
        "peak_simulators": 3,
    }


def test_taxi_attempt_isolation_accepts_captured_run(validator) -> None:
    validator.validate_attempt_isolation(CAPTURED / "timeline.json", CAPTURED / "campaign")


def test_taxi_continuation_accepts_captured_run(validator) -> None:
    campaign = CAPTURED / "campaign"
    validator.validate_continue_after_failure(
        campaign / "manifest.json",
        campaign / "summary.json",
        sorted(campaign.glob("work-items/*/result.json")),
    )


def test_taxi_heavy_cap_rejects_overshoot_between_samples(validator, tmp_path: Path) -> None:
    """Two child claims overlap in [5, 7) while no sample looks: three slots under a cap of two."""
    intervals = [(None, 1, 3), ("child-a", 2, 4), ("child-b", 6, 8)]
    timeline = tmp_path / "timeline.json"
    _write_json(
        timeline,
        {
            "outer_execution_id": OUTER,
            "slot_samples": [
                _sample(2, [OUTER, "child-a"], []),
                _sample(8, [OUTER, "child-b"], []),
            ],
            "final_slot_state": {"heavy_holders": [], "heavy_waiters": []},
            "claim_transitions": [
                {"at_ns": 2, "child_execution_id": "child-a", "state": "submitted"},
                {"at_ns": 2, "child_execution_id": "child-a", "state": "promoted"},
                {"at_ns": 5, "child_execution_id": "child-b", "state": "submitted"},
                {"at_ns": 5, "child_execution_id": "child-b", "state": "promoted"},
                {"at_ns": 7, "child_execution_id": "child-a", "state": "released"},
                {"at_ns": 9, "child_execution_id": "child-b", "state": "released"},
            ],
            "intervals": [
                {
                    "attempt_id": f"attempt-{index}",
                    "child_execution_id": child,
                    "start_ns": start,
                    "end_ns": end,
                }
                for index, (child, start, end) in enumerate(intervals)
            ],
        },
    )
    with pytest.raises(ValueError, match="claim transitions exceed max_heavy"):
        validator.validate_heavy_cap(timeline, 2)
    assert validator.validate_heavy_cap(timeline, 3) == {"max_heavy": 3, "peak_simulators": 2}


def _unobserved_process(document) -> None:
    document["slot_samples"] = [
        sample for sample in document["slot_samples"] if sample["at_ns"] not in (9, 10)
    ]
    document["intervals"][2].update(start_ns=8, end_ns=10)


def _drop_promotion(document) -> None:
    document["claim_transitions"] = [
        item
        for item in document["claim_transitions"]
        if not (item["child_execution_id"] == CHILDREN[1] and item["state"] == "promoted")
    ]


def _claim_for_borrowed_lane(document) -> None:
    document["intervals"][0]["child_execution_id"] = "0" * 32


def _unclaimed_child_lane(document) -> None:
    document["intervals"][1]["child_execution_id"] = None


def _outer_absent_while_running(document) -> None:
    document["slot_samples"][5]["heavy_holders"].remove(OUTER)


def _early_child_process(document) -> None:
    document["intervals"][2]["start_ns"] = 3


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_drop_promotion, "exact release"),
        (
            lambda document: document["slot_samples"][4]["heavy_holders"].append("over-cap"),
            "exceed max_heavy",
        ),
        (_claim_for_borrowed_lane, "bind every exact child"),
        (_unclaimed_child_lane, "bind every exact child"),
        (_outer_absent_while_running, "omits the borrowed outer Job"),
        (_early_child_process, "started before promotion"),
        (_unobserved_process, "no SlotStore sample observed a simulator process"),
        (
            lambda document: document["final_slot_state"]["heavy_holders"].append(OUTER),
            "final SlotStore inspection",
        ),
        (lambda document: document["slot_samples"][6].update(at_ns=8), "contradicts transitions"),
    ],
)
def test_taxi_heavy_cap_rejects_corrupt_slot_accounting(
    validator, evidence, mutation, message
) -> None:
    timeline, _campaign = evidence
    _rewrite(timeline, mutation)
    with pytest.raises(ValueError, match=message):
        validator.validate_heavy_cap(timeline, 3)


def test_taxi_heavy_cap_rejects_two_processes_on_the_borrowed_slot(validator, evidence) -> None:
    timeline, _campaign = evidence

    def share_outer_slot(document) -> None:
        child = document["intervals"][1]["child_execution_id"]
        document["intervals"][1]["child_execution_id"] = None
        document["claim_transitions"] = [
            item for item in document["claim_transitions"] if item["child_execution_id"] != child
        ]
        for sample in document["slot_samples"]:
            for key in ("heavy_holders", "heavy_waiters"):
                sample[key] = [value for value in sample[key] if value != child]

    _rewrite(timeline, share_outer_slot)
    with pytest.raises(ValueError, match="overlap on the borrowed outer slot"):
        validator.validate_heavy_cap(timeline, 3)


def test_taxi_attempt_isolation_rejects_tampered_evidence(validator, evidence) -> None:
    timeline, campaign = evidence
    retained = timeline.parent / "outputs" / ATTEMPTS[0] / "qa-shared-name.txt"
    original = retained.read_bytes()
    retained.write_text("forged\n", encoding="utf-8")
    with pytest.raises(ValueError, match="output retained digest differs"):
        validator.validate_attempt_isolation(timeline, campaign)
    retained.write_bytes(original)

    log = _attempt_directory(campaign, 1) / "evidence/0001-run.log"
    original = log.read_bytes()
    log.write_bytes(original.replace(b"done", b"gone"))
    with pytest.raises(ValueError, match="evidence retained digest differs"):
        validator.validate_attempt_isolation(timeline, campaign)
    log.write_bytes(original)
    validator.validate_attempt_isolation(timeline, campaign)


def _contaminate_log(campaign: Path) -> None:
    log = _attempt_directory(campaign, 1) / "evidence/0001-run.log"
    raw = log.read_bytes() + b"CMD: snapshot/Vcampaign_tb +test=slow-last\n"
    log.write_bytes(raw)
    _rewrite(
        _work_item_directory(campaign, 1) / "result.json",
        lambda document: document["evidence"][0].update(bytes=len(raw), sha256=_digest(raw)),
    )


def _disown_run_directory(campaign: Path) -> None:
    _rewrite(
        _attempt_directory(campaign, 2) / "attempt.json",
        lambda document: document["run_directory"].update(owned=False),
    )


def _share_run_directory(campaign: Path) -> None:
    _rewrite(
        _attempt_directory(campaign, 2) / "attempt.json",
        lambda document: document["run_directory"].update(resolved=_run_directory(1)),
    )


def _swap_child(campaign: Path) -> None:
    _rewrite(
        _attempt_directory(campaign, 0) / "attempt.json",
        lambda document: document.update(child_execution_id=CHILDREN[1]),
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_contaminate_log, "log evidence is contaminated"),
        (_disown_run_directory, "not attempt-owned"),
        (_share_run_directory, "differs from the attempt"),
        (_swap_child, "attempt child identity differs"),
    ],
)
def test_taxi_attempt_isolation_rejects_inconsistent_campaign_records(
    validator, evidence, mutation, message
) -> None:
    timeline, campaign = evidence
    mutation(campaign)
    with pytest.raises(ValueError, match=message):
        validator.validate_attempt_isolation(timeline, campaign)


def _misname_attempt_directory(campaign: Path) -> None:
    directory = _attempt_directory(campaign, 0)
    directory.rename(directory.with_name("0001-someone-else"))


def _duplicate_attempt(campaign: Path) -> None:
    source = _attempt_directory(campaign, 0)
    target = _work_item_directory(campaign, 1) / "attempts" / f"0002-{ATTEMPTS[0]}"
    target.mkdir()
    (target / "attempt.json").write_bytes((source / "attempt.json").read_bytes())


def _remove_attempt_records(campaign: Path) -> None:
    for record in campaign.glob("work-items/*/attempts/*/attempt.json"):
        record.unlink()


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_misname_attempt_directory, "does not name its Simulation Attempt"),
        (_duplicate_attempt, "Simulation Attempt identity repeats"),
        (_remove_attempt_records, "Campaign has no Simulation Attempts"),
    ],
)
def test_taxi_attempt_isolation_rejects_broken_attempt_layout(
    validator, evidence, mutation, message
) -> None:
    timeline, campaign = evidence
    mutation(campaign)
    with pytest.raises(ValueError, match=message):
        validator.validate_attempt_isolation(timeline, campaign)


def _retarget_result(document) -> None:
    document["manifest_sha256"] = "sha256:" + "a" * 64


@pytest.mark.parametrize(
    ("document", "mutation", "message"),
    [
        (
            "result",
            lambda value: value["observations"][0].update(test="slow-last"),
            "result order",
        ),
        ("result", lambda value: value.update(grade="pass"), "terminal grades differ"),
        ("result", _retarget_result, "do not authenticate"),
        ("result", lambda value: value["evidence"][0].update(owner=ATTEMPTS[0]), "evidence owner"),
        ("result", lambda value: value["observations"].append({}), "one observation"),
        ("summary", lambda value: value.update(aggregate_grade="pass"), "strict failure"),
        ("summary", lambda value: value["completed"].reverse(), "completion identities"),
        ("summary", lambda value: value.update(complete=False), "unfinished work"),
        ("manifest", lambda value: value.update(created_at="later"), "do not authenticate"),
    ],
)
def test_taxi_continuation_rejects_tampered_campaign_records(
    validator, evidence, document, mutation, message
) -> None:
    _timeline, campaign = evidence
    results = [_work_item_directory(campaign, index) / "result.json" for index in range(3)]
    paths = {
        "result": results[1],
        "summary": campaign / "summary.json",
        "manifest": campaign / "manifest.json",
    }
    _rewrite(paths[document], mutation)
    with pytest.raises(ValueError, match=message):
        validator.validate_continue_after_failure(
            campaign / "manifest.json", campaign / "summary.json", results
        )


def test_uart_literal_cwd_validator_proves_scoped_serialization(tmp_path: Path) -> None:
    validator = _module("uart_phase4_validator", UART / "validate_campaign.py")
    manifest = tmp_path / "manifest.json"
    timeline = tmp_path / "timeline.json"
    _write_json(manifest, {"workload": {"run_cwd": {"kind": "literal"}}})
    _write_json(
        timeline,
        {
            "shared_intervals": [
                {"run_directory": "shared", "start_ns": 1, "end_ns": 4},
                {"run_directory": "shared", "start_ns": 4, "end_ns": 8},
            ],
            "unrelated_intervals": [{"run_directory": "isolated", "start_ns": 2, "end_ns": 6}],
        },
    )
    validator.validate_literal_cwd_serialization(manifest, timeline)
    document = json.loads(timeline.read_text())
    document["shared_intervals"][1]["start_ns"] = 3
    _write_json(timeline, document)
    with pytest.raises(ValueError, match="attempts overlap"):
        validator.validate_literal_cwd_serialization(manifest, timeline)


def test_phase4_fixture_policy_is_explicit() -> None:
    assert (
        'run_cwd = ".booley-qa/campaign/{campaign}/{test}/{attempt}"'
        in (TAXI / "parallel.toml").read_text()
    )
    assert (
        'run_cwd = ".booley-qa/campaign/literal-shared"' in (UART / "literal-cwd.toml").read_text()
    )
    taxi_source = (TAXI / "campaign_main.cpp").read_text()
    assert "milliseconds(2000)" in taxi_source
    assert 'ofstream marker("qa-shared-name.txt"' in taxi_source
    uart_source = (UART / "record_literal_cwd.py").read_text()
    assert "time.sleep(2.0)" in uart_source
    assert "time.monotonic_ns()" in uart_source
