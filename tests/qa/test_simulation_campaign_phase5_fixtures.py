"""Contract tests for Phase 5 Simulation Campaign Public QA assets."""

from __future__ import annotations

import hashlib
import importlib.util
import json
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TAXI_VALIDATOR = ROOT / "qa/missions/taxi/fixtures/simulation-campaign/validate_phase5.py"
# Documents captured unchanged from one Taxi QA run.
CAPTURED = ROOT / "tests/qa/fixtures/taxi_campaign"


def _module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n")
    return path


def _digest(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _rewrite(path: Path, mutate: Callable[[dict], object]) -> None:
    document = json.loads(path.read_text())
    mutate(document)
    _write(path, document)


@pytest.fixture(scope="module")
def taxi():
    return _module(TAXI_VALIDATOR, "taxi_phase5")


CAMPAIGN_ID = "c37cba48-7128-4aa8-a2a2-1a4220793161"
WORKLOAD = "sha256:" + "9" * 64
BATCH_ITEM = "item:0000:3a7c5369a7156296"
BATCH_TESTS = ["alpha", "beta"]
ATTEMPT_IDS = ("7852faf4-7c93-4f9f-8e21-8bb0952b34f8", "0142c525-7188-45a2-9aee-d60f83c18664")


def _manifest(names_per_item: list[list[str]], kind: str) -> dict[str, object]:
    return {
        "$schema": "booley.simulation-campaign-manifest/v3",
        "campaign_id": CAMPAIGN_ID,
        "fingerprints": {"workload_sha256": WORKLOAD},
        "work_items": [
            {
                "kind": kind,
                "ordinal": ordinal,
                "selection": {"kind": "named", "names": names},
                "work_item_id": f"item:{ordinal:04d}:3a7c5369a7156296",
            }
            for ordinal, names in enumerate(names_per_item)
        ],
    }


def _batch_directory(campaign: Path) -> Path:
    return campaign / "work-items/0001-3a7c5369a7156296"


def _batch_attempt(campaign: Path, ordinal: int) -> Path:
    name = f"{ordinal:04d}-{ATTEMPT_IDS[ordinal - 1]}"
    return _batch_directory(campaign) / "attempts" / name / "attempt.json"


def _taxi_batch_files(tmp_path: Path) -> tuple[Path, Path]:
    """Write one resumed Cocotb-batch Campaign and the pre-resume attempt copy."""
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    manifest = _write(campaign / "manifest.json", _manifest([BATCH_TESTS], "cocotb_batch"))
    identity = {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": _digest(manifest.read_bytes().rstrip(b"\n")),
        "workload_sha256": WORKLOAD,
        "work_item_id": BATCH_ITEM,
    }
    for ordinal, attempt_id in enumerate(ATTEMPT_IDS, start=1):
        record = _batch_attempt(campaign, ordinal)
        record.parent.mkdir(parents=True)
        attempt = {"$schema": "booley.simulation-attempt/v1", **identity}
        _write(record, {**attempt, "attempt_id": attempt_id, "attempt_ordinal": ordinal})
    _write(
        _batch_directory(campaign) / "result.json",
        {
            "$schema": "booley.simulation-result/v2",
            **identity,
            "attempt_id": ATTEMPT_IDS[1],
            "attempt_ordinal": 2,
            "state": "completed",
            "grade": "pass",
            "observations": [{"test": name} for name in BATCH_TESTS],
        },
    )
    interrupted = tmp_path / "interrupted-attempt.json"
    interrupted.write_bytes(_batch_attempt(campaign, 1).read_bytes())
    return campaign, interrupted


def test_taxi_batch_validator_accepts_interrupted_then_resumed_batch(taxi, tmp_path: Path) -> None:
    taxi.validate_cocotb_batch(*_taxi_batch_files(tmp_path))


def test_taxi_batch_validator_accepts_captured_run(taxi) -> None:
    taxi.validate_cocotb_batch(
        CAPTURED / "batch/campaign", CAPTURED / "batch/interrupted-attempt.json"
    )


def _result_mutation(mutate: Callable[[dict], object]) -> Callable[[Path], None]:
    return lambda campaign: _rewrite(_batch_directory(campaign) / "result.json", mutate)


def _attempt_mutation(ordinal: int, mutate: Callable[[dict], object]) -> Callable[[Path], None]:
    return lambda campaign: _rewrite(_batch_attempt(campaign, ordinal), mutate)


def _reuse_attempt(campaign: Path) -> None:
    record = _batch_attempt(campaign, 2)
    _rewrite(record, lambda attempt: attempt.update(attempt_id=ATTEMPT_IDS[0]))
    record.parent.rename(record.parent.with_name(f"0002-{ATTEMPT_IDS[0]}"))


def _drop_resumed_attempt(campaign: Path) -> None:
    record = _batch_attempt(campaign, 2)
    record.unlink()
    record.parent.rmdir()


def _bind_result_to_interrupted_attempt(result: dict) -> None:
    result.update(attempt_id=ATTEMPT_IDS[0], attempt_ordinal=1)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_attempt_mutation(1, lambda value: value.update(campaign_id="other")), "do not bind"),
        (
            _attempt_mutation(2, lambda value: value.update(manifest_sha256="sha256:" + "a" * 64)),
            "do not bind",
        ),
        (
            _attempt_mutation(2, lambda value: value.update(work_item_id="item:0001:x")),
            "do not bind",
        ),
        (_attempt_mutation(2, lambda value: value.update(attempt_ordinal=1)), "ordinals do not"),
        (_attempt_mutation(2, lambda value: value.update(**{"$schema": "v0"})), "attempt schema"),
        (_reuse_attempt, "reused an attempt"),
        (_drop_resumed_attempt, "one interrupted and one resumed attempt"),
        (_result_mutation(_bind_result_to_interrupted_attempt), "bind the resumed attempt"),
        (_result_mutation(lambda value: value.update(campaign_id="other")), "resumed result does"),
        (_result_mutation(lambda value: value.update(state="aborted")), "did not complete"),
        (_result_mutation(lambda value: value["observations"].pop()), "no per-test observations"),
        (_result_mutation(lambda value: value["observations"].reverse()), "differ from exact"),
        (
            lambda campaign: _rewrite(campaign / "manifest.json", lambda value: value.update(x=1)),
            "do not bind",
        ),
    ],
)
def test_taxi_batch_validator_rejects_unbound_or_partial_retry(
    taxi, tmp_path: Path, mutation: Callable[[Path], None], message: str
) -> None:
    campaign, interrupted = _taxi_batch_files(tmp_path)
    mutation(campaign)
    with pytest.raises(ValueError, match=message):
        taxi.validate_cocotb_batch(campaign, interrupted)


def test_taxi_batch_validator_rejects_foreign_interrupted_attempt_copy(
    taxi, tmp_path: Path
) -> None:
    campaign, interrupted = _taxi_batch_files(tmp_path)
    _rewrite(interrupted, lambda attempt: attempt.update(campaign_id="other"))
    with pytest.raises(ValueError, match="copy differs from the Campaign's first attempt"):
        taxi.validate_cocotb_batch(campaign, interrupted)


MCP_KINDS = {
    "manifest": "simulation_campaign_manifest",
    "simulation": "simulation_projection",
    "coverage": "coverage_campaign_reference",
}


def _retain(invocation: Path, key: str, raw: bytes) -> dict[str, object]:
    """Retain one Campaign artifact below the invocation and return its typed reference."""
    relative = {
        "manifest": "targets/taxi/campaign/manifest.json",
        "simulation": "targets/taxi/simulation.json",
        "coverage": "targets/taxi/coverage.json",
    }[key]
    retained = invocation / relative
    retained.parent.mkdir(parents=True, exist_ok=True)
    retained.write_bytes(raw)
    return {
        "bytes": len(raw),
        "kind": MCP_KINDS[key],
        "owner": CAMPAIGN_ID,
        "path": relative,
        "path_base": "report_invocation",
        "sha256": _digest(raw),
    }


def _mcp_observations(names: list[str]) -> dict[str, object]:
    preview = [
        {
            "test": name,
            "execution": "completed",
            "failure_class": None,
            "functional": "pass",
            "assertions": "clean",
            "assertion_count": 0,
            "cycle_count": None,
            "detail": {"reason": name},
        }
        for name in names[:32]
    ]
    return {
        "observations": preview,
        "observation_total": len(names),
        "observations_truncated": len(names) > 32,
        "observation_counts": {
            "execution": {"completed": len(names)},
            "functional": {"pass": len(names)},
            "assertions": {"clean": len(names)},
        },
        "termination_counts": {},
        "build_stage": [],
    }


def _mcp_campaign(invocation: Path, names: list[str]) -> dict[str, object]:
    manifest = (json.dumps(_manifest([[name] for name in names], "ordinary_hdl")) + "\n").encode()
    projection = json.dumps({"campaign_id": CAMPAIGN_ID, "passed": True}).encode()
    return {
        "campaign_id": CAMPAIGN_ID,
        "manifest_sha256": _digest(manifest.rstrip(b"\n")),
        "artifacts": {
            "manifest": _retain(invocation, "manifest", manifest),
            "simulation": _retain(invocation, "simulation", projection),
        },
        "grade": "pass",
        "complete": True,
        **_mcp_observations(names),
    }


def _mcp_envelope(campaign: dict[str, object], structured_key: str) -> dict[str, object]:
    report = {"exit_code": 0, "detail": {"campaigns": {"taxi": campaign}}}
    return {
        "meta": None,
        "content": [{"type": "text", "text": "EXIT_CODE: 0\nPASS"}],
        "is_error": False,
        structured_key: {"reports": [report]},
    }


def _mcp_response(
    tmp_path: Path,
    mutate: Callable[[dict], object] = lambda campaign: None,
    *,
    names: tuple[str, ...] = ("alpha", "beta"),
    structured_key: str = "structured_content",
) -> tuple[Path, Path]:
    """Write one MCP response and the artifacts it references; return it and its invocation."""
    invocation = tmp_path / "flow-reports/sim/3"
    campaign = _mcp_campaign(invocation, list(names))
    mutate(campaign)
    return _write(tmp_path / "mcp.json", _mcp_envelope(campaign, structured_key)), invocation


def _validate_mcp(taxi, response: Path, invocation: Path, **locations: Path) -> None:
    taxi.validate_mcp_response(response, response.stat().st_size, invocation, **locations)


@pytest.mark.parametrize("structured_key", ["structured_content", "structuredContent"])
def test_taxi_mcp_validator_accepts_authenticated_response(
    taxi, tmp_path: Path, structured_key: str
) -> None:
    response, invocation = _mcp_response(tmp_path, structured_key=structured_key)
    _validate_mcp(taxi, response, invocation)
    with pytest.raises(ValueError, match="declared bound"):
        taxi.validate_mcp_response(response, response.stat().st_size - 1, invocation)


@pytest.mark.parametrize(
    ("capture", "invocation"),
    [
        ("ordered-mcp.txt", "3"),
        ("ordered-mcp-alias-normalized.json", "3"),
        ("taxi-ordered-mcp.txt", "4"),
    ],
)
def test_taxi_mcp_validator_accepts_captured_responses(
    taxi, capture: str, invocation: str
) -> None:
    _validate_mcp(taxi, CAPTURED / "mcp" / capture, CAPTURED / "mcp/flow-reports/sim" / invocation)


def test_taxi_mcp_validator_accepts_truncated_preview_of_full_selection(
    taxi, tmp_path: Path
) -> None:
    names = tuple(f"test-{index:02d}" for index in range(40))
    response, invocation = _mcp_response(tmp_path, names=names)
    _validate_mcp(taxi, response, invocation)


def test_taxi_mcp_validator_accepts_inconclusive_grade(taxi, tmp_path: Path) -> None:
    response, invocation = _mcp_response(
        tmp_path, lambda campaign: campaign.update(grade="inconclusive")
    )
    _validate_mcp(taxi, response, invocation)


def test_taxi_mcp_validator_authenticates_optional_coverage_artifact(taxi, tmp_path: Path) -> None:
    invocation = tmp_path / "flow-reports/sim/3"
    coverage = _retain(invocation, "coverage", b'{"coverage": 1}\n')
    response, _ = _mcp_response(
        tmp_path, lambda campaign: campaign["artifacts"].update(coverage=coverage)
    )
    _validate_mcp(taxi, response, invocation)
    (invocation / "targets/taxi/coverage.json").write_bytes(b'{"coverage": 2}\n')
    with pytest.raises(ValueError, match="coverage artifact reference retained digest differs"):
        _validate_mcp(taxi, response, invocation)


def _rebase(base: str, path: str) -> Callable[[dict], None]:
    def mutate(campaign: dict) -> None:
        for reference in campaign["artifacts"].values():
            reference.update(path_base=base, path=reference["path"].replace("targets/taxi", path))

    return mutate


def test_taxi_mcp_validator_resolves_every_reference_base(taxi, tmp_path: Path) -> None:
    response, invocation = _mcp_response(tmp_path, _rebase("reports_root", "sim/3/targets/taxi"))
    _validate_mcp(taxi, response, invocation)

    response, invocation = _mcp_response(tmp_path, _rebase("external_origin_target", "."))
    origin_target = invocation / "targets/taxi"
    _validate_mcp(taxi, response, invocation, external_origin_target=origin_target)
    with pytest.raises(ValueError, match="base location is not retained"):
        _validate_mcp(taxi, response, invocation)
    with pytest.raises(ValueError, match="file is not retained"):
        _validate_mcp(taxi, response, invocation, external_origin_target=invocation)


def _retype_manifest(campaign) -> None:
    campaign["artifacts"]["manifest"]["kind"] = "simulation_projection"


def _escape_projection(campaign) -> None:
    campaign["artifacts"]["simulation"]["path"] = "../simulation.json"


def _omit_selected_test(campaign) -> None:
    campaign.update(_mcp_observations(["alpha"]))


def _reorder_selected_tests(campaign) -> None:
    campaign["observations"].reverse()


def _rename_campaign(campaign) -> None:
    campaign["campaign_id"] = "foreign"
    for reference in campaign["artifacts"].values():
        reference["owner"] = "foreign"


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda campaign: campaign["artifacts"].pop("manifest"),
            "manifest artifact reference is missing",
        ),
        (
            lambda campaign: campaign["artifacts"].update(manifest="campaign/manifest.json"),
            "not a typed artifact reference",
        ),
        (_retype_manifest, "manifest artifact reference kind differs"),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(owner="other"),
            "simulation artifact reference owner differs",
        ),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(path_base="origin_target"),
            "simulation artifact reference base is invalid",
        ),
        (_escape_projection, "simulation artifact reference path escapes"),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(sha256="sha256:0"),
            "simulation artifact reference digest is invalid",
        ),
        (
            lambda campaign: campaign["artifacts"]["manifest"].update(bytes=0),
            "manifest artifact reference size is invalid",
        ),
        (
            lambda campaign: campaign["artifacts"]["manifest"].update(bytes=True),
            "manifest artifact reference size is invalid",
        ),
        (
            lambda campaign: campaign["artifacts"].update(summary=dict(campaign["artifacts"])),
            "artifacts are unexpected",
        ),
        (
            lambda campaign: campaign["artifacts"]["manifest"].update(path="targets/absent.json"),
            "manifest artifact reference file is not retained",
        ),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(
                sha256="sha256:" + "0" * 64
            ),
            "simulation artifact reference retained digest differs",
        ),
        (
            lambda campaign: campaign["artifacts"]["simulation"].update(bytes=7),
            "simulation artifact reference retained size differs",
        ),
        (_omit_selected_test, "total differs from the manifest selection"),
        (_reorder_selected_tests, "observations differ from the manifest selection"),
        (_rename_campaign, "names a foreign Campaign"),
        (
            lambda campaign: campaign.update(manifest_sha256="sha256:" + "b" * 64),
            "digest differs from the retained manifest",
        ),
        (lambda campaign: campaign["observations"][0].pop("cycle_count"), "fields differ"),
        (
            lambda campaign: campaign["observations"][0].update(failure_class="flaky"),
            "failure class is invalid",
        ),
        (
            lambda campaign: campaign["observations"][0].update(cycle_count=-1),
            "cycle count is invalid",
        ),
        (lambda campaign: campaign.update(grade="unknown"), "campaign grade is invalid"),
        (lambda campaign: campaign.update(observation_total=3), "preview length"),
        (lambda campaign: campaign.update(observations_truncated=True), "truncation flag"),
        (lambda campaign: campaign["observations"][0].pop("detail"), "fields differ"),
        (
            lambda campaign: campaign["observation_counts"]["functional"].update(fail=1),
            "counts differ from preview",
        ),
    ],
)
def test_taxi_mcp_validator_rejects_unauthenticated_or_inconsistent_response(
    taxi, tmp_path: Path, mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    response, invocation = _mcp_response(tmp_path, mutation)
    with pytest.raises(ValueError, match=message):
        _validate_mcp(taxi, response, invocation)


def test_taxi_mcp_validator_rejects_symlinked_artifact(taxi, tmp_path: Path) -> None:
    response, invocation = _mcp_response(tmp_path)
    projection = invocation / "targets/taxi/simulation.json"
    projection.rename(projection.with_name("elsewhere.json"))
    projection.symlink_to("elsewhere.json")
    with pytest.raises(ValueError, match="simulation artifact reference file is not retained"):
        _validate_mcp(taxi, response, invocation)


def _coverage_refusal_files(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    manifest = _write(
        tmp_path / "manifest.json",
        {"work_items": [{"kind": "coverage_aggregate", "work_item_id": "item:0000:coverage"}]},
    )
    interrupted_attempt = _write(
        tmp_path / "interrupted-attempt.json",
        {
            "$schema": "booley.simulation-attempt/v1",
            "work_item_id": "item:0000:coverage",
            "attempt_id": "first",
        },
    )
    rejection = _write(
        tmp_path / "rejection.json",
        {
            "exit_code": 2,
            "dry_run_exit_code": 2,
            "diagnostic": "start a new run: booley flow sim --target sim_0 --coverage",
            "attempts_before": ["first"],
            "attempts_after": ["first"],
            "eda_launches": 0,
            "control_exit_code": 0,
        },
    )
    return manifest, interrupted_attempt, tmp_path / "interrupted-result.json", rejection


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(exit_code=0), "did not exit 2"),
        (lambda value: value.update(dry_run_exit_code=0), "dry-run did not exit 2"),
        (lambda value: value.update(attempts_after=["first", "second"]), "attempt inventory"),
        (
            lambda value: (value.pop("attempts_before"), value.pop("attempts_after")),
            "omitted the attempt inventory",
        ),
        (lambda value: value.update(diagnostic="resume failed"), "new coverage run"),
        (lambda value: value.update(eda_launches=1), "launched EDA"),
    ],
)
def test_coverage_phase5_validator_requires_resume_refusal(
    tmp_path: Path, mutation: Callable[[dict[str, object]], object], message: str
) -> None:
    module = _module(
        ROOT / "qa/shared/coverage/simulation-campaign/validate_aggregate.py",
        "coverage_phase5",
    )
    manifest, attempt, result, rejection = _coverage_refusal_files(tmp_path)
    module.validate(manifest, attempt, result, rejection)

    document = json.loads(rejection.read_text())
    mutation(document)
    _write(rejection, document)
    with pytest.raises(ValueError, match=message):
        module.validate(manifest, attempt, result, rejection)

    _write(rejection, json.loads(_coverage_refusal_files(tmp_path)[3].read_text()))
    result.write_text("{}")
    with pytest.raises(ValueError, match="terminal result"):
        module.validate(manifest, attempt, result, rejection)
