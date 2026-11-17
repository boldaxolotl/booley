"""Current Coverage Record persistence through its public filesystem seam."""

from __future__ import annotations

import gzip
import json
import os
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from booley.flows.sim import coverage_record_store as store
from booley.flows.sim.coverage_json import (
    MANIFEST_JSON,
    content_digest,
    coverage_mapping_document,
    freeze_coverage_mapping,
)
from booley.flows.sim.coverage_policy import (
    ApprovedWaiverSet,
    CoverageCriterion,
    CoverageThreshold,
    coverage_metrics,
    evaluate_coverage_record,
)
from booley.flows.sim.coverage_record import (
    CoveragePoint,
    CoveragePointIdentity,
    CoverageRecordValidationError,
    DurableTargetIdentity,
    derive_coverage_rollups,
    encode_coverage_point,
    encode_coverage_point_id,
)
from booley.flows.sim.coverage_record_store import (
    COVERAGE_RECORD_SCHEMA,
    MAX_MANIFEST_BYTES,
    CoverageRecordStoreError,
    load_coverage_record,
    load_coverage_record_bytes,
    publish_coverage_record,
)
from booley.flows.sim.record.codec import UnsupportedSimulationRecordFormatError
from tests.flows.sim.coverage_record_test_support import decode_document, rewrite_manifest_as_v4
from tests.flows.sim.test_coverage_record import _valid_document
from tests.flows.sim.test_simulation_record_unsupported_formats import (
    _assert_refused,
    _assert_unsupported_type,
)
from tests.runtime.test_sandbox_layout import project_alias

__all__ = ["project_alias"]

TARGET = DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")


def _coverage_record():
    return decode_document(_valid_document(), TARGET)


@pytest.mark.parametrize("boundary", ["manifest", "jsonl"])
def test_nested_coverage_exact_and_over_limit_preserve_authoritative_hook_sidecars(
    tmp_path, monkeypatch, boundary
):
    from tests.flows.sim.test_endpoint_record_lifecycle import _successful_pre_sim_record

    _invocation, outcome = _successful_pre_sim_record(tmp_path, monkeypatch, "immutable")
    firing = outcome.pre_sim_firings[0]
    before = firing.path.read_bytes()
    attempt = firing.path.parent.parent
    coverage_record = _coverage_record()
    runs = tuple(
        replace(
            run,
            attributes=freeze_coverage_mapping(
                {
                    **dict(run.attributes),
                    "pre_sim": {
                        "status": "passed",
                        "returncode": 0,
                        "command_count": 2,
                        "elapsed_s": 0.1,
                        "stdout_tail": "s" * 8192,
                        "stderr_tail": "e" * 8192,
                    },
                }
            ),
        )
        for run in coverage_record.runs
    )
    coverage_record = replace(coverage_record, runs=runs)
    measured = publish_coverage_record(attempt / "measured", coverage_record)
    if boundary == "manifest":
        limit, constant = measured.coverage_record.stat().st_size, "MAX_MANIFEST_BYTES"
    else:
        limit = len(gzip.decompress(measured.points.read_bytes()))
        constant = "MAX_UNCOMPRESSED_BYTES"
    monkeypatch.setattr(store, constant, limit)
    exact = publish_coverage_record(attempt / "exact", coverage_record)
    assert exact.coverage_record.is_file()
    monkeypatch.setattr(store, constant, limit - 1)
    with pytest.raises(CoverageRecordStoreError):
        publish_coverage_record(attempt / "over", coverage_record)
    assert firing.path.read_bytes() == before
    assert not (attempt / "over/coverage.json").exists()
    assert not (attempt / "over/coverage-points.jsonl.gz").exists()


def _invalid_coverage_record():
    coverage_record = _coverage_record()
    return replace(
        coverage_record,
        normalization=freeze_coverage_mapping({"status": "partial", "unrecognized_records": []}),
        rollups=(),
        collection=freeze_coverage_mapping(
            {
                "status": "collector_error",
                "merge": {"status": "not_run", "artifact": None},
                "diagnostics": [],
            }
        ),
    )


def _large_coverage_record(point_count: int, covered: int | None = None):
    """Build *point_count* eligible line points; the first *covered* are hit (default all)."""
    base = _coverage_record()
    covered = point_count if covered is None else covered
    points = []
    for index in range(point_count):
        identity = CoveragePointIdentity(
            metric="line",
            location=freeze_coverage_mapping(
                {
                    "source": "rtl/counter.sv",
                    "start": {"line": index + 1, "column": 1},
                    "end": {"line": index + 1, "column": 2},
                }
            ),
            hierarchy="TOP.counter",
            subject=freeze_coverage_mapping({"basic_block": index}),
            collector=freeze_coverage_mapping(
                {"record_type": "v_line", "native_key": f"line:{index}"}
            ),
        )
        point = CoveragePoint(
            "pending",
            identity,
            {"run:reset": 1} if index < covered else {},
            freeze_coverage_mapping({"kind": "eligible"}),
        )
        encoded_identity = encode_coverage_point(point)["identity"]
        assert isinstance(encoded_identity, dict)
        points.append(replace(point, id=encode_coverage_point_id(encoded_identity)))
    frozen = tuple(points)
    return replace(base, points=frozen, rollups=derive_coverage_rollups(frozen))


def _evaluated_coverage_record(
    covered: int, total: int, minimum: Fraction, *, scored: bool = True
):
    """Evaluate *covered* of *total* line points against a line floor of *minimum* percent.

    Without scored capability attributes the evaluation is ``blocked``.
    """
    coverage_record = _large_coverage_record(total, covered)
    attributes = {"collection": "supported", "scoring": "scored_v1"} if scored else {}
    capabilities = tuple(
        replace(capability, attributes=freeze_coverage_mapping(attributes))
        for capability in coverage_record.collector.capabilities
    )
    coverage_record = replace(
        coverage_record, collector=replace(coverage_record.collector, capabilities=capabilities)
    )
    criterion = CoverageCriterion(TARGET, (CoverageThreshold("line", minimum),), None)
    waivers = ApprovedWaiverSet(
        freeze_coverage_mapping({"status": "disabled"}), "sha256:empty", ()
    )
    return evaluate_coverage_record(coverage_record, criterion, waivers)


def _point(
    *, source: str, metric: str, line: int, hits: int, disposition: str, hierarchy: str
) -> CoveragePoint:
    identity = CoveragePointIdentity(
        metric=metric,
        location=freeze_coverage_mapping(
            {
                "source": source,
                "start": {"line": line, "column": 1},
                "end": {"line": line, "column": 2},
            }
        ),
        hierarchy=hierarchy,
        subject=freeze_coverage_mapping({"outcome": line}),
        collector=freeze_coverage_mapping(
            {"record_type": f"v_{metric}", "native_key": f"{source}:{metric}:{line}"}
        ),
    )
    point = CoveragePoint(
        "pending",
        identity,
        {"run:reset": hits} if hits else {},
        freeze_coverage_mapping({"kind": disposition}),
    )
    encoded_identity = encode_coverage_point(point)["identity"]
    assert isinstance(encoded_identity, dict)
    return replace(point, id=encode_coverage_point_id(encoded_identity))


def _source_coverage_record():
    base = _coverage_record()
    rows = (
        ("rtl/z.sv", "line", 1, 1, "eligible", "TOP.first"),
        ("rtl/z.sv", "line", 2, 0, "waived", "TOP.second"),
        ("rtl/a.sv", "branch", 3, 0, "eligible", "TOP.third"),
        ("tb/bench.sv", "expression", 4, 1, "unscored", "TOP.tb"),
        ("rtl/a.sv", "toggle", 5, 2, "eligible", "TOP.fourth"),
        ("rtl/a.sv", "cover_property", 6, 1, "eligible", "TOP.fifth"),
    )
    points = tuple(
        _point(
            source=source,
            metric=metric,
            line=line,
            hits=hits,
            disposition=disposition,
            hierarchy=hierarchy,
        )
        for source, metric, line, hits, disposition, hierarchy in rows
    )
    return replace(base, points=points, rollups=derive_coverage_rollups(points))


def test_publish_separates_summary_from_lossless_points(tmp_path: Path) -> None:
    paths = publish_coverage_record(tmp_path, _coverage_record())

    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    assert manifest["$schema"] == COVERAGE_RECORD_SCHEMA
    assert manifest["scoring"] == {"status": "valid", "reason": None}
    assert "points" not in manifest
    assert manifest["rollups"][0]["percent"] == 100.0
    assert manifest["evaluation"]["status"] == "not_requested"
    assert manifest["point_store"]["path"] == "coverage-points.jsonl.gz"
    assert manifest["point_store"]["point_count"] == 1
    assert [item["source"] for item in manifest["source_rollups"]] == ["rtl/counter.sv"]
    assert [item["metric"] for item in manifest["source_rollups"][0]["rollups"]] == [
        "line",
        "branch",
        "expression",
        "toggle",
    ]
    assert manifest["source_rollups"][0]["rollups"][0]["percent"] == 100.0
    assert paths.points.is_file()

    loaded = load_coverage_record(paths.coverage_record, TARGET)
    assert loaded.summary.document["source_rollups"][0]["source"] == "rtl/counter.sv"
    assert loaded.coverage_record == _coverage_record()
    assert not hasattr(loaded.coverage_record, "schema")


def test_invalid_scoring_summary_and_deep_load_retain_points(tmp_path: Path) -> None:
    paths = publish_coverage_record(tmp_path, _invalid_coverage_record())
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))

    assert manifest["scoring"] == {"status": "invalid", "reason": "collector_error"}
    assert manifest["rollups"] == []
    assert manifest["source_rollups"] == []
    loaded = load_coverage_record(paths.coverage_record, TARGET)
    assert loaded.summary.document["scoring"] == {"reason": "collector_error", "status": "invalid"}
    assert loaded.coverage_record.points
    assert loaded.coverage_record.rollups == ()


def test_manifest_has_canonical_per_source_rollups(tmp_path: Path) -> None:
    paths = publish_coverage_record(tmp_path, _source_coverage_record())
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))

    assert [item["source"] for item in manifest["source_rollups"]] == [
        "rtl/a.sv",
        "rtl/z.sv",
        "tb/bench.sv",
    ]
    assert all(
        [rollup["metric"] for rollup in source["rollups"]]
        == ["line", "branch", "expression", "toggle"]
        for source in manifest["source_rollups"]
    )
    by_source = {item["source"]: item["rollups"] for item in manifest["source_rollups"]}
    z_line = by_source["rtl/z.sv"][0]
    assert z_line == {
        "metric": "line",
        "semantics": (
            "One Verilator basic-block point; covered when its count is greater than zero."
        ),
        "total_points": 2,
        "eligible_points": 1,
        "covered_points": 1,
        "waived_points": 1,
        "percent": 100.0,
    }
    assert by_source["rtl/a.sv"][1]["percent"] == 0.0
    assert by_source["rtl/a.sv"][3]["percent"] == 100.0
    assert by_source["tb/bench.sv"][2]["total_points"] == 1
    assert by_source["tb/bench.sv"][2]["eligible_points"] == 0
    assert by_source["tb/bench.sv"][2]["percent"] is None
    assert all(
        rollup["metric"] != "cover_property"
        for source in manifest["source_rollups"]
        for rollup in source["rollups"]
    )


def test_publish_rejects_oversized_manifest_and_removes_point_store(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(store, "MAX_MANIFEST_BYTES", 1)

    with pytest.raises(CoverageRecordStoreError) as error:
        publish_coverage_record(tmp_path, _coverage_record())

    assert error.value.code == "COV_MANIFEST_LIMIT"
    assert not (tmp_path / "coverage.json").exists()
    assert not (tmp_path / "coverage-points.jsonl.gz").exists()


def test_publish_decodes_nothing_it_just_encoded(tmp_path: Path, monkeypatch) -> None:
    def refuse_decode(*args, **kwargs):
        raise AssertionError("publication must not decode the manifest it just encoded")

    with monkeypatch.context() as patched:
        patched.setattr(store, "decode_coverage_record", refuse_decode)
        paths = publish_coverage_record(tmp_path, _coverage_record())

    assert (
        load_coverage_record(paths.coverage_record, TARGET).coverage_record == _coverage_record()
    )


def test_failed_manifest_link_removes_point_store(tmp_path: Path, monkeypatch) -> None:
    real_link = os.link

    def link_unless_manifest(source, destination, *args, **kwargs):
        if Path(destination).name == "coverage.json":
            raise OSError("injected manifest link failure")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "link", link_unless_manifest)

    with pytest.raises(OSError, match="injected manifest link failure"):
        publish_coverage_record(tmp_path, _coverage_record())

    assert not (tmp_path / "coverage.json").exists()
    assert not (tmp_path / "coverage-points.jsonl.gz").exists()


@pytest.mark.parametrize("name", ["coverage-points.jsonl.gz", "coverage.json"])
def test_publish_never_replaces_an_existing_coverage_record_file(
    tmp_path: Path, name: str
) -> None:
    existing = tmp_path / name
    existing.write_bytes(b"owned")

    with pytest.raises(CoverageRecordStoreError, match="cannot replace"):
        publish_coverage_record(tmp_path, _coverage_record())

    assert existing.read_bytes() == b"owned"
    if name == "coverage.json":
        assert not (tmp_path / "coverage-points.jsonl.gz").exists()


def test_deep_reader_rejects_truncated_point_storage(tmp_path: Path) -> None:
    paths = publish_coverage_record(tmp_path, _coverage_record())
    paths.points.write_bytes(paths.points.read_bytes()[:-8])

    with pytest.raises(CoverageRecordStoreError) as error:
        load_coverage_record(paths.coverage_record, TARGET)
    assert error.value.code == "COV_POINT_FORMAT"


def test_deep_reader_stops_at_the_uncompressed_ceiling(tmp_path: Path, monkeypatch) -> None:
    paths = publish_coverage_record(tmp_path, _coverage_record())
    ceiling = json.loads(paths.coverage_record.read_bytes())["point_store"]["uncompressed_bytes"]
    monkeypatch.setattr(store, "MAX_UNCOMPRESSED_BYTES", ceiling)
    load_coverage_record(paths.coverage_record, TARGET)
    paths.points.write_bytes(gzip.compress(b" " * (ceiling + 1), mtime=0))

    with pytest.raises(CoverageRecordStoreError) as error:
        load_coverage_record(paths.coverage_record, TARGET)
    assert error.value.code == "COV_POINT_LIMIT"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink setup requires host privileges")
def test_deep_reader_rejects_linked_point_storage(tmp_path: Path) -> None:
    paths = publish_coverage_record(tmp_path, _coverage_record())
    retained = tmp_path / "retained.gz"
    paths.points.rename(retained)
    paths.points.symlink_to(retained.name)

    with pytest.raises(CoverageRecordStoreError) as error:
        load_coverage_record(paths.coverage_record, TARGET)
    assert error.value.code == "COV_PATH_UNSAFE"


@pytest.mark.skipif(os.name == "nt", reason="Windows symlink setup requires host privileges")
def test_manifest_reader_rejects_linked_parent(tmp_path: Path) -> None:
    retained = tmp_path / "retained"
    paths = publish_coverage_record(retained, _coverage_record())
    linked = tmp_path / "linked"
    linked.symlink_to(retained, target_is_directory=True)

    with pytest.raises(CoverageRecordStoreError) as error:
        load_coverage_record(linked / paths.coverage_record.name, TARGET)

    assert error.value.code == "COV_PATH_UNSAFE"


def test_deep_load_validates_the_coverage_record_once(tmp_path: Path, monkeypatch) -> None:
    import booley.flows.sim.coverage_record as codec

    # Validation runs inside the one decode; no public seam can tell one validation from two.
    paths = publish_coverage_record(tmp_path, _coverage_record())
    structural = codec._structural_findings
    calls = 0

    def counted(document, points):
        nonlocal calls
        calls += 1
        return structural(document, points)

    monkeypatch.setattr(codec, "_structural_findings", counted)
    load_coverage_record(paths.coverage_record, TARGET)
    assert calls == 1


@pytest.mark.parametrize(
    ("honest", "forged"),
    [("not_requested", "pass"), ("blocked", "pass"), ("blocked", "fail")],
)
def test_deep_load_rejects_a_forged_verdict(tmp_path: Path, honest: str, forged: str) -> None:
    coverage_record = (
        _coverage_record()
        if honest == "not_requested"
        else _evaluated_coverage_record(1, 2, Fraction(50), scored=False)
    )
    assert coverage_record.evaluation["status"] == honest
    paths = publish_coverage_record(tmp_path, coverage_record)
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    manifest["evaluation"]["status"] = forged
    paths.coverage_record.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CoverageRecordValidationError) as caught:
        load_coverage_record(paths.coverage_record, TARGET)

    assert [
        (finding.code, finding.pointer, finding.message) for finding in caught.value.findings
    ] == [
        (
            "COV_EVALUATION_MISMATCH",
            "/evaluation",
            "Stored evaluation does not match the exact deterministic derivation.",
        )
    ]


def _load_bytes(path: Path, target: DurableTargetIdentity):
    return load_coverage_record_bytes(path, path.read_bytes(), target)


@pytest.mark.parametrize("reader", [load_coverage_record, _load_bytes])
@pytest.mark.parametrize("version", ["v1", "v2", "v3", "v4", "v5"])
def test_readers_refuse_every_coverage_record_booley_0_4_0_wrote_as_older(
    tmp_path: Path, reader, version: str
) -> None:
    path = tmp_path / "coverage.json"
    document = _valid_document()
    document["$schema"] = f"booley.coverage-campaign/{version}"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(UnsupportedSimulationRecordFormatError, match="older Booley") as error:
        reader(path, TARGET)

    _assert_refused(str(error.value), document["$schema"])
    _assert_unsupported_type(error.value)


@pytest.mark.parametrize("reader", [load_coverage_record, _load_bytes])
@pytest.mark.parametrize(
    "schema",
    ["booley.coverage-record/v2", [], {}, 5, None],
    ids=["v2", "list", "dict", "int", "none"],
)
def test_readers_report_an_unknown_or_non_string_schema_as_unsupported(
    tmp_path: Path, reader, schema: object
) -> None:
    path = tmp_path / "coverage.json"
    document = _valid_document()
    document["$schema"] = schema
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CoverageRecordStoreError) as error:
        reader(path, TARGET)

    assert error.value.code == "COV_SCHEMA_VERSION_UNSUPPORTED"
    if schema == "booley.coverage-record/v2":
        assert "recollect coverage" in str(error.value)


def test_unsupported_schema_is_rejected_before_coverage_record_decode(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(_valid_document()), encoding="utf-8")
    decode = store.decode_coverage_record
    calls = 0

    def counted_decode(manifest, points, expected_target):
        nonlocal calls
        calls += 1
        return decode(manifest, points, expected_target)

    monkeypatch.setattr(store, "decode_coverage_record", counted_decode)

    with pytest.raises(CoverageRecordStoreError) as error:
        load_coverage_record(path, TARGET)

    assert error.value.code == "COV_SCHEMA_VERSION_UNSUPPORTED"
    assert calls == 0


def test_large_coverage_record_keeps_manifest_and_summary_work_constant(tmp_path: Path) -> None:
    small = publish_coverage_record(tmp_path / "small", _coverage_record())
    large_coverage_record = _large_coverage_record(40_000)
    large = publish_coverage_record(tmp_path / "large", large_coverage_record)
    small_manifest = small.coverage_record.read_bytes()
    large_manifest = large.coverage_record.read_bytes()

    point_store = json.loads(large_manifest)["point_store"]

    assert len(large_manifest) - len(small_manifest) < 128
    assert point_store["point_count"] == 40_000
    assert large.points.stat().st_size < point_store["uncompressed_bytes"]


def test_image_alias_store_readers_and_suffix_links(project_alias):
    alias, target = project_alias
    published = publish_coverage_record(target / "record", _coverage_record())
    path = alias / "record/coverage.json"
    assert (
        load_coverage_record(path).coverage_record
        == load_coverage_record(published.coverage_record).coverage_record
    )
    assert (
        load_coverage_record_bytes(path, published.coverage_record.read_bytes()).coverage_record
        == _coverage_record()
    )
    (target / "linked").symlink_to(target / "record", target_is_directory=True)
    for read in (
        load_coverage_record,
        lambda p: load_coverage_record_bytes(p, published.coverage_record.read_bytes()),
    ):
        with pytest.raises((ValueError, OSError), match=r"link|regular|unsafe"):
            read(alias / "linked/coverage.json")


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_store_follows_only_the_booley_dir_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    """Publish and both readers cross the symlinked ``.booley`` entry, nothing else (#2192)."""
    from tests.conftest import symlink_or_skip
    from tests.fusesoc_test_support import symlinked_booley_dir

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    external = symlinked_booley_dir(project, tmp_path / "external-booley")
    if linked == "below":
        (tmp_path / "elsewhere").mkdir()
        symlink_or_skip(
            external / "flow-reports", tmp_path / "elsewhere", target_is_directory=True
        )
    target_dir = project / ".booley/flow-reports/sim/1/targets/sim"

    if linked == "below":
        with pytest.raises(CoverageRecordStoreError, match="contains a link"):
            publish_coverage_record(target_dir, _coverage_record())
        return
    paths = publish_coverage_record(target_dir, _coverage_record())
    assert (
        load_coverage_record(paths.coverage_record, TARGET).coverage_record == _coverage_record()
    )
    assert _load_bytes(paths.coverage_record, TARGET).coverage_record == _coverage_record()


def _publish_coverage_record_as_written_by_booley_0_3_0(root: Path):
    """Publish a Coverage Record, then rewrite its manifest into what Booley 0.3.0 wrote."""
    paths = publish_coverage_record(root, _coverage_record())
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    rewrite_manifest_as_v4(manifest)
    paths.coverage_record.write_text(json.dumps(manifest), encoding="utf-8")
    return paths


def test_a_coverage_record_written_by_booley_0_3_0_is_refused_as_older(
    tmp_path: Path,
) -> None:
    paths = _publish_coverage_record_as_written_by_booley_0_3_0(tmp_path)

    with pytest.raises(UnsupportedSimulationRecordFormatError) as error:
        load_coverage_record(paths.coverage_record, TARGET)

    # Outside a report layout no invocation directory can be named.
    assert "booley.coverage-campaign/v4 was written by an older Booley" in str(error.value)


@pytest.mark.parametrize(
    "dropped",
    [
        pytest.param(("test_run",), id="test_run"),
        pytest.param(("variant",), id="variant"),
        pytest.param(("test_run", "variant", "seed", "fingerprint"), id="all"),
    ],
)
def test_a_run_without_its_test_run_identity_is_refused(
    tmp_path: Path, dropped: tuple[str, ...]
) -> None:
    paths = publish_coverage_record(tmp_path, _coverage_record())
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    for field in dropped:
        del manifest["tests"]["runs"][0][field]
    paths.coverage_record.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(CoverageRecordValidationError) as error:
        load_coverage_record(paths.coverage_record, TARGET)

    findings = {(finding.code, finding.pointer) for finding in error.value.findings}
    for field in dropped:
        assert ("COV_FIELD_REQUIRED", f"/tests/runs/0/{field}") in findings


# Pinned on the base commit, before the load recomputed anything, and re-pinned for the
# Coverage Record names of #3688 (only the schema, point store schema and identity key
# changed): the sha256 of the manifest Booley then wrote for
# ``_evaluated_coverage_record(1, 2, Fraction(50))``, with the zlib-dependent
# ``point_store.sha256`` and ``point_store.bytes`` dropped.
_PRE_CHANGE_MANIFEST_SHA256 = (
    "sha256:5c6f2c0842a7ef78408c3e51c0f823257608eb019235c726a101dcde968886b1"
)


def test_a_scored_coverage_record_written_before_the_change_reloads_unchanged(
    tmp_path: Path,
) -> None:
    evaluated = _evaluated_coverage_record(1, 2, Fraction(50))
    assert evaluated.evaluation["status"] == "pass"
    paths = publish_coverage_record(tmp_path, evaluated)
    stored = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    pinned = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    for volatile in ("sha256", "bytes"):
        del pinned["point_store"][volatile]
    assert content_digest(MANIFEST_JSON.encode(pinned)) == _PRE_CHANGE_MANIFEST_SHA256

    loaded = load_coverage_record(paths.coverage_record, TARGET)

    summary = coverage_mapping_document(loaded.summary.document)
    assert loaded.coverage_record == evaluated
    assert summary["rollups"] == stored["rollups"]
    assert summary["evaluation"] == stored["evaluation"]
    assert coverage_metrics(loaded.coverage_record) == coverage_metrics(evaluated)


@pytest.mark.parametrize(
    ("covered", "total", "minimum", "verdict"),
    [
        pytest.param(1, 2, "50", "pass", id="exact-hit"),
        pytest.param(1, 3, "33.33", "pass", id="third-passes"),
        pytest.param(2, 3, "66.67", "fail", id="two-thirds-fails"),
        # Float 0.1 is above 1/10, so reading the stored value without str() would fail this.
        pytest.param(1, 1000, "0.1", "pass", id="decimal-exact-hit"),
    ],
)
def test_reload_rescores_threshold_edges_like_the_criterion(
    tmp_path: Path, covered: int, total: int, minimum: str, verdict: str
) -> None:
    evaluated = _evaluated_coverage_record(covered, total, Fraction(minimum))
    assert evaluated.evaluation["status"] == verdict
    paths = publish_coverage_record(tmp_path, evaluated)
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    # Tamper with every stored derived value: rollup, row verdict and status.
    manifest["rollups"][0]["covered_points"] = 0
    manifest["rollups"][0]["percent"] = 0.0
    flipped = "fail" if verdict == "pass" else "pass"
    manifest["evaluation"]["metrics"][0]["verdict"] = flipped
    manifest["evaluation"]["status"] = flipped
    paths.coverage_record.write_text(json.dumps(manifest), encoding="utf-8")

    loaded = load_coverage_record(paths.coverage_record, TARGET)

    assert loaded.coverage_record.evaluation["status"] == verdict
    assert loaded.coverage_record == evaluated
    summary = coverage_mapping_document(loaded.summary.document)
    assert summary["evaluation"]["status"] == verdict
    assert summary["rollups"][0]["covered_points"] == covered


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("missing-field", "COV_MANIFEST_FORMAT"),
        ("extra-field", "COV_MANIFEST_FORMAT"),
        ("oversized", "COV_MANIFEST_LIMIT"),
        ("huge-integer", "COV_MANIFEST_FORMAT"),
    ],
)
def test_manifest_field_set_and_read_ceiling_keep_their_codes(
    tmp_path: Path, case: str, code: str
) -> None:
    paths = publish_coverage_record(tmp_path, _coverage_record())
    manifest = json.loads(paths.coverage_record.read_text(encoding="utf-8"))
    if case == "missing-field":
        del manifest["findings"]
    elif case == "extra-field":
        manifest["unexpected"] = 1
    raw = json.dumps(manifest).encode()
    if case == "oversized":
        raw = b" " * (MAX_MANIFEST_BYTES + 1)
    elif case == "huge-integer":
        # Python refuses to convert an integer of more than 4,300 digits.
        raw = b'{"$schema": 1' + b"0" * 5_000 + b"}"
    paths.coverage_record.write_bytes(raw)

    for reader in (load_coverage_record, _load_bytes):
        with pytest.raises(CoverageRecordStoreError) as error:
            reader(paths.coverage_record, TARGET)
        assert error.value.code == code
