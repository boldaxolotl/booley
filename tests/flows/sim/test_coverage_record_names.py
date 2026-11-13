"""The Coverage Record, its reference, availability and analysis report carry Coverage Record names.

Part 5 of ADR 0090. Booley 0.4.0's Coverage Campaign formats are read from committed bytes
that Booley 0.4.0's own ``SimulateFlow`` wrote with ``--coverage``: the reference is
``tests/flows/fixtures/booley_0_4_0/coverage/sim/1/targets/sim_0/coverage.json`` and the
Coverage Record is the ``coverage-campaign/`` directory of its one work-item attempt
(``coverage.json`` and ``coverage-points.jsonl.gz``). Both were captured by #3685 and are
never edited here; a test that needs one at the current layout copies its bytes.
"""

from __future__ import annotations

import gzip
import json
import types
from pathlib import Path
from typing import Any

import pytest

from booley.flows.sim.campaign.codec import canonical_json_bytes
from booley.flows.sim.campaign_retention import (
    CampaignRetentionError,
    prune_invocation,
    prune_native_payload,
)
from booley.flows.sim.coverage_analysis_input import CoverageAnalysisError, read_coverage_campaign
from booley.flows.sim.coverage_campaign import DurableTargetIdentity
from booley.flows.sim.coverage_campaign_store import (
    CoverageCampaignStoreError,
    load_coverage_campaign,
    load_coverage_campaign_bytes,
)
from booley.flows.sim.coverage_json import content_digest
from booley.flows.sim.coverage_reference import (
    CoverageCampaignReferenceError,
    authenticate_coverage_campaign_owner,
    decode_coverage_campaign_reference,
    resolve_coverage_campaign_reference,
)
from booley.harness import booley as cli
from booley.specialists.coverage_analysis import CoverageAnalysisError as AnalystError
from booley.specialists.coverage_analysis import CoverageAnalyzer
from tests.flows.sim.campaign_test_support import CoverageInvocation, coverage_invocation
from tests.flows.sim.coverage_campaign_test_support import decode_document
from tests.flows.sim.test_campaign_unsupported_formats import (
    _assert_refused,
    _assert_unsupported_type,
)
from tests.flows.sim.test_campaign_unsupported_formats import (
    reports as _reports_fixture,
)
from tests.flows.sim.test_coverage_campaign import _valid_document
from tests.flows.sim.test_coverage_campaign_store import TARGET, _evaluated_campaign
from tests.harness.test_coverage_cli import _booley

# The report-root fixture of the Part 1 tests, collected here under the same name.
reports = _reports_fixture

_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "booley_0_4_0"
    / "coverage"
    / "sim"
    / "1"
    / "targets"
    / "sim_0"
)
_OLD_RECORD = "booley.coverage-campaign/v5"
_OLD_REFERENCE = "booley.coverage-campaign-reference/v1"
_RECORD = "booley.coverage-record/v1"
_REFERENCE = "booley.coverage-record-reference/v1"
_POINTS = "booley.coverage-points/v2"
_NO_FACTS = {"hypotheses": [], "recommendations": [], "waiver_candidates": []}


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_bytes())


def _rewrite(path: Path, raw: bytes) -> None:
    path.chmod(0o600)
    path.write_bytes(raw)


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under *root* by relative path, to show a refusal changed nothing."""
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.suffix != ".lock"
    }


def _0_4_0_reference() -> bytes:
    raw = (_FIXTURE / "coverage.json").read_bytes()
    assert json.loads(raw)["$schema"] == _OLD_REFERENCE
    return raw


def _0_4_0_record() -> tuple[bytes, bytes]:
    """The 0.4.0 Coverage Record manifest bytes and its point store bytes."""
    (directory,) = _FIXTURE.glob("campaign/work-items/*/attempts/*/coverage-campaign")
    manifest = (directory / "coverage.json").read_bytes()
    assert json.loads(manifest)["$schema"] == _OLD_RECORD
    return manifest, (directory / "coverage-points.jsonl.gz").read_bytes()


def _current_reference(run: CoverageInvocation) -> dict[str, Any]:
    reference = _json(run.public)
    assert reference["$schema"] == _REFERENCE, reference["$schema"]
    return reference


def _install_0_4_0_record(run: CoverageInvocation) -> None:
    """Replace a current run's Coverage Record with 0.4.0 bytes, binding the reference to them."""
    manifest, points = _0_4_0_record()
    reference = _current_reference(run)
    reference["coverage_record"].update(bytes=len(manifest), sha256=content_digest(manifest))
    _rewrite(run.campaign_path, manifest)
    _rewrite(run.campaign_path.with_name("coverage-points.jsonl.gz"), points)
    _rewrite(run.public, canonical_json_bytes(reference))


def _run_analysis(summary_from: Path | None = None) -> dict[str, Any]:
    """The analysis report of the Coverage Record at *summary_from*, or of an in-memory one."""
    if summary_from is None:
        document = _valid_document()
        campaign = decode_document(document, DurableTargetIdentity(document["target"]["identity"]))
        summary = None
    else:
        loaded = load_coverage_campaign(summary_from, TARGET)
        campaign, summary = loaded.campaign, loaded.summary
    analyzer = CoverageAnalyzer(lambda _prompt: _NO_FACTS)
    return analyzer.analyze_coverage_campaign(campaign, None, "", summary=summary).to_dict()


# --- AC1: the names a run writes -----------------------------------------------------------


def test_a_coverage_run_writes_coverage_record_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    simulation_record_id = _json(run.target / "record" / "manifest.json")["simulation_record_id"]

    reference = _current_reference(run)
    assert reference["simulation_record_id"] == simulation_record_id
    record = _json(run.campaign_path)
    assert reference["coverage_record"]["coverage_record_id"] == record["coverage_record_id"]
    assert reference["coverage_record"]["schema"] == _RECORD
    for old in ("simulation_campaign_id", "coverage_campaign"):
        assert old not in reference
    assert "campaign_id" not in reference["coverage_record"]

    assert record["$schema"] == _RECORD
    assert record["point_store"]["$schema"] == _POINTS
    assert "campaign_id" not in record
    with gzip.open(run.campaign_path.with_name("coverage-points.jsonl.gz")) as stream:
        header = json.loads(stream.readline())
    assert header == {
        "$schema": _POINTS,
        "coverage_record_id": record["coverage_record_id"],
        "target_identity": record["target"]["identity"],
    }

    # The Simulation result names the nested record under the Coverage Record evidence kind.
    (result,) = run.target.glob("record/work-items/*/result.json")
    kinds = {item["kind"] for item in _json(result)["evidence"]}
    assert "coverage_record_manifest" in kinds
    assert "coverage_campaign_manifest" not in kinds

    # Native pruning leaves an availability record under the same names.
    prune_native_payload(run.reports, 1, "sim_0")
    availability = _json(run.storage / "availability.json")
    assert availability["$schema"] == "booley.coverage-availability/v2"
    assert availability["coverage_record_id"] == record["coverage_record_id"]
    assert availability["coverage_record_sha256"] == content_digest(
        run.campaign_path.read_bytes()
    ).removeprefix("sha256:")
    assert "campaign_id" not in availability
    assert "campaign_sha256" not in availability


def test_the_coverage_analysis_report_names_the_coverage_record(tmp_path: Path) -> None:
    from booley.flows.sim.coverage_campaign_store import publish_coverage_campaign

    # A stored Coverage Record: the report carries its manifest and point store digest.
    paths = publish_coverage_campaign(tmp_path, _evaluated_campaign(1, 2, 50))
    stored = _run_analysis(paths.campaign)
    manifest = _json(paths.campaign)
    assert stored["$schema"] == "booley.coverage-analysis/v4", stored["$schema"]
    assert stored["coverage_record_id"] == manifest["coverage_record_id"]
    assert stored["observed_evidence"]["coverage_record_manifest"]["$schema"] == _RECORD
    assert "campaign_manifest" not in stored["observed_evidence"]
    assert "campaign_id" not in stored

    # An in-memory Coverage Record has no stored summary and is the document itself.
    memory = _run_analysis()
    assert memory["$schema"] == "booley.coverage-analysis/v3"
    assert memory["observed_evidence"]["$schema"] == "booley.coverage-record-document/v1"
    assert memory["observed_evidence"]["coverage_record_id"] == memory["coverage_record_id"]
    assert "campaign_id" not in memory["observed_evidence"]

    # The model is given the same names.
    prompts: list[str] = []
    document = _valid_document()
    campaign = decode_document(document, DurableTargetIdentity(document["target"]["identity"]))

    def model(prompt: str) -> dict[str, list[object]]:
        prompts.append(prompt)
        return _NO_FACTS

    CoverageAnalyzer(model).analyze_coverage_campaign(campaign, None, "")
    assert '"coverage_record_reference"' in prompts[0]
    assert '"coverage_record_id"' in prompts[0]
    assert '"campaign_reference"' not in prompts[0]


def test_an_unknown_cp1_reference_is_reported_as_not_in_the_record() -> None:
    foreign_id = (
        "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6IjE6Zm9yZWlnbiIsInJlY29yZF90eX"
        "BlIjoidl9saW5lIn0sImhpZXJhcmNoeSI6IlRPUC5mb3JlaWduIiwibG9jYXRpb24iOnsiZW"
        "5kIjp7ImNvbHVtbiI6MiwibGluZSI6MX0sInNvdXJjZSI6InJ0bC9mb3JlaWduLnN2Iiwic3"
        "RhcnQiOnsiY29sdW1uIjoxLCJsaW5lIjoxfX0sIm1ldHJpYyI6ImxpbmUiLCJzdWJqZWN0Ij"
        "p7ImJhc2ljX2Jsb2NrIjowfX0"
    )
    document = _valid_document()
    campaign = decode_document(document, DurableTargetIdentity(document["target"]["identity"]))
    response = {
        "hypotheses": [{"point_ids": [foreign_id], "explanation": "foreign"}],
        "recommendations": [],
        "waiver_candidates": [],
    }

    with pytest.raises(AnalystError) as captured:
        CoverageAnalyzer(lambda _prompt: response).analyze_coverage_campaign(campaign, None, "")

    message = str(captured.value)
    assert "/hypotheses/0/point_ids/0: cp1_not_in_record" in message
    assert "cp1_not_in_campaign" not in message


# --- AC2: every 0.4.0 Coverage Record and reference is refused as older ----------------------


def test_a_0_4_0_coverage_record_is_refused_naming_its_invocation(reports: Path) -> None:
    manifest, points = _0_4_0_record()
    invocation = reports / "sim" / "1"
    storage = (
        invocation
        / "targets"
        / "sim_0"
        / "record"
        / "work-items"
        / "0001-c73ae78ad98f38c2"
        / "attempts"
        / "0001-031ec997-4d89-4f71-bbc9-7e99a591bb75"
        / "coverage-record"
    )
    storage.mkdir(parents=True)
    (storage / "coverage.json").write_bytes(manifest)
    (storage / "coverage-points.jsonl.gz").write_bytes(points)

    with pytest.raises(ValueError) as refused:
        load_coverage_campaign(storage / "coverage.json")

    _assert_unsupported_type(refused.value)
    _assert_refused(str(refused.value), _OLD_RECORD, invocation)

    # The same bytes at a path whose layout names no invocation are still refused as old.
    with pytest.raises(ValueError) as unlocated:
        load_coverage_campaign_bytes(storage.parent / "coverage.json", manifest)
    _assert_unsupported_type(unlocated.value)
    _assert_refused(str(unlocated.value), _OLD_RECORD)


def test_a_0_4_0_coverage_record_reference_is_refused_naming_its_invocation(
    reports: Path,
) -> None:
    raw = _0_4_0_reference()

    # The decoder sees bytes only, so it cannot name a directory.
    with pytest.raises(ValueError) as decoded:
        decode_coverage_campaign_reference(raw)
    _assert_unsupported_type(decoded.value)
    _assert_refused(str(decoded.value), _OLD_REFERENCE)

    # Resolving the Target file names the invocation to delete.
    public = reports / "sim" / "1" / "targets" / "sim_0" / "coverage.json"
    public.parent.mkdir(parents=True)
    public.write_bytes(raw)
    with pytest.raises(ValueError) as resolved:
        resolve_coverage_campaign_reference(public)
    _assert_unsupported_type(resolved.value)
    _assert_refused(str(resolved.value), _OLD_REFERENCE, reports / "sim" / "1")


# --- AC3: analysis, `booley coverage` and retention report it as older, not corrupt ----------


def test_coverage_analysis_reports_a_0_4_0_reference_as_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    _current_reference(run)
    _rewrite(run.public, _0_4_0_reference())

    with pytest.raises(CoverageAnalysisError) as analysed:
        read_coverage_campaign(run.public)

    _assert_refused(str(analysed.value), _OLD_REFERENCE, run.reports / "sim" / "1")
    assert "fully pruned" not in str(analysed.value)


def test_coverage_analysis_reports_a_0_4_0_coverage_record_as_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    _install_0_4_0_record(run)

    with pytest.raises(CoverageAnalysisError) as analysed:
        read_coverage_campaign(run.public)

    _assert_refused(str(analysed.value), _OLD_RECORD, run.reports / "sim" / "1")
    assert "fully pruned" not in str(analysed.value)


def test_booley_coverage_reports_a_0_4_0_reference_as_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    _current_reference(run)
    _rewrite(run.public, _0_4_0_reference())
    # A host terminal: no Sandbox or Project is present.
    monkeypatch.setattr(cli.runtime_context, "inside_session_runtime", lambda: False)

    code, _out, err = _booley(monkeypatch, capsys, "coverage", "uncovered", str(run.public))

    assert code == 2
    _assert_refused(err, _OLD_REFERENCE, run.reports / "sim" / "1")


def test_retention_reports_a_0_4_0_reference_as_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    _current_reference(run)
    _rewrite(run.public, _0_4_0_reference())
    invocation = run.reports / "sim" / "1"
    before = _tree(invocation)

    with pytest.raises(CampaignRetentionError) as full:
        prune_invocation(run.reports, 1)
    _assert_refused(str(full.value), _OLD_REFERENCE, invocation)

    with pytest.raises(CampaignRetentionError) as native:
        prune_native_payload(run.reports, 1, "sim_0")
    _assert_refused(str(native.value), _OLD_REFERENCE, invocation)
    assert _tree(invocation) == before


def test_retention_reports_a_0_4_0_coverage_record_as_older(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    _install_0_4_0_record(run)
    invocation = run.reports / "sim" / "1"
    before = _tree(invocation)

    with pytest.raises(CampaignRetentionError) as full:
        prune_invocation(run.reports, 1)

    _assert_refused(str(full.value), _OLD_RECORD, invocation)
    assert _tree(invocation) == before


# --- AC4: no old key, schema, evidence kind or category is an alias ---------------------------


def _record_alias_refusals(run: CoverageInvocation) -> list[str]:
    """Refusals of a Coverage Record that keeps an old key or point store schema."""
    record_path = run.campaign_path
    points_path = record_path.with_name("coverage-points.jsonl.gz")
    record = _json(record_path)
    assert record["$schema"] == _RECORD
    refusals: list[str] = []

    # The old identity key under the current schema is not an exact Coverage Record.
    aliased = {k: v for k, v in record.items() if k != "coverage_record_id"}
    aliased["campaign_id"] = record["coverage_record_id"]
    with pytest.raises(CoverageCampaignStoreError) as keyed:
        load_coverage_campaign_bytes(record_path, canonical_json_bytes(aliased))
    refusals.append(str(keyed.value))

    # The old point store schema in the manifest.
    stale_store = json.loads(json.dumps(record))
    stale_store["point_store"]["$schema"] = "booley.coverage-points/v1"
    with pytest.raises(CoverageCampaignStoreError) as stale:
        load_coverage_campaign_bytes(record_path, canonical_json_bytes(stale_store))
    refusals.append(str(stale.value))

    # The old identity key in the point store header.
    genuine_points = points_path.read_bytes()
    with gzip.open(points_path) as stream:
        lines = stream.read().splitlines(keepends=True)
    old_header = {
        "$schema": _POINTS,
        "campaign_id": record["coverage_record_id"],
        "target_identity": record["target"]["identity"],
    }
    points_path.write_bytes(
        gzip.compress(canonical_json_bytes(old_header) + b"".join(lines[1:]), mtime=0)
    )
    with pytest.raises(CoverageCampaignStoreError) as header:
        load_coverage_campaign_bytes(record_path, record_path.read_bytes())
    refusals.append(str(header.value))
    points_path.write_bytes(genuine_points)
    return refusals


def _reference_alias_refusals(reference: dict[str, Any]) -> list[str]:
    """Refusals of a current-schema reference that keeps an old key."""
    refusals: list[str] = []
    for rename, nested in (
        (("simulation_record_id", "simulation_campaign_id"), False),
        (("coverage_record", "coverage_campaign"), False),
        (("coverage_record_id", "campaign_id"), True),
    ):
        value = json.loads(json.dumps(reference))
        holder = value["coverage_record"] if nested else value
        holder[rename[1]] = holder.pop(rename[0])
        with pytest.raises(CoverageCampaignReferenceError, match="exact fields") as exact:
            decode_coverage_campaign_reference(canonical_json_bytes(value))
        refusals.append(str(exact.value))
    return refusals


def _evidence_kind_refusal(run: CoverageInvocation, monkeypatch: pytest.MonkeyPatch) -> str:
    """The refusal when the enclosing result names the nested record by the old evidence kind."""
    resolved = resolve_coverage_campaign_reference(run.public)
    genuine = authenticate_coverage_campaign_owner(resolved)
    evidence = genuine.result.document["evidence"]
    assert any(item["kind"] == "coverage_record_manifest" for item in evidence)
    forged = types.SimpleNamespace(
        manifest=genuine.manifest,
        manifest_sha256=genuine.manifest_sha256,
        result=types.SimpleNamespace(
            document={
                **genuine.result.document,
                "evidence": [
                    {**item, "kind": "coverage_campaign_manifest"}
                    if item["kind"] == "coverage_record_manifest"
                    else item
                    for item in evidence
                ],
            }
        ),
    )
    monkeypatch.setattr(
        "booley.flows.sim.campaign.authenticate_work_item", lambda *_a, **_k: forged
    )
    with pytest.raises(CoverageCampaignReferenceError, match="disagrees with reference") as kind:
        authenticate_coverage_campaign_owner(resolved)
    return str(kind.value)


def test_old_coverage_record_names_are_not_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = coverage_invocation(tmp_path, monkeypatch)
    reference = _current_reference(run)
    record = _json(run.campaign_path)
    refusals = [
        *_record_alias_refusals(run),
        *_reference_alias_refusals(reference),
        _evidence_kind_refusal(run, monkeypatch),
    ]

    # Booley 0.4.0's schema IDs, even with every new key, are older, never an alias.
    for schema in (
        "booley.coverage-campaign/v1",
        "booley.coverage-campaign/v2",
        "booley.coverage-campaign/v3",
        "booley.coverage-campaign/v4",
        _OLD_RECORD,
    ):
        with pytest.raises(ValueError) as older:
            load_coverage_campaign_bytes(
                run.campaign_path, canonical_json_bytes({**record, "$schema": schema})
            )
        _assert_unsupported_type(older.value)
        _assert_refused(str(older.value), schema)
    with pytest.raises(ValueError) as older_reference:
        decode_coverage_campaign_reference(
            canonical_json_bytes({**reference, "$schema": _OLD_REFERENCE})
        )
    _assert_unsupported_type(older_reference.value)
    _assert_refused(str(older_reference.value), _OLD_REFERENCE)

    assert refusals
    assert all("older Booley" not in text for text in refusals)
