"""Hostile public-seam tests for Simulation-owned Coverage Record references."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from booley.flows.sim import coverage_reference
from booley.flows.sim.coverage_record import (
    CoverageRecordValidationError,
    DurableTargetIdentity,
)
from booley.flows.sim.coverage_record_store import (
    publish_coverage_record,
)
from booley.flows.sim.coverage_reference import (
    MAX_REFERENCE_BYTES,
    CoverageRecordReference,
    CoverageRecordReferenceError,
    build_coverage_record_reference,
    decode_coverage_record_reference,
    encode_coverage_record_reference,
    publish_coverage_record_reference,
    resolve_coverage_record_reference,
)
from booley.flows.sim.record.codec import UnsupportedSimulationRecordFormatError
from tests.flows.sim.coverage_record_test_support import (
    corrupt_coverage_record_with_duplicate_negative_point,
    decode_document,
)
from tests.flows.sim.test_coverage_record import _valid_document
from tests.runtime.test_sandbox_layout import project_alias

__all__ = ["project_alias"]

_SIMULATION_RECORD_ID = "12345678-1234-4234-9234-123456789abc"
_SIMULATION_ATTEMPT_ID = "87654321-4321-4432-a234-cba987654321"
_WORK_ITEM_ID = "item:0000:0123456789abcdef"
_TARGET_IDENTITY = "acme:demo:counter:1.0#sim_counter"
_TARGET_SELECTOR = "sim_counter"
_MANIFEST_SHA256 = "sha256:" + "a" * 64
_ORIGIN_INVOCATION_ID = 12


def _nested_coverage_record(
    origin_target: Path, *, attempt_id: str = _SIMULATION_ATTEMPT_ID
) -> Path:
    attempt = (
        origin_target
        / "record"
        / "work-items"
        / "0001-0123456789abcdef"
        / "attempts"
        / f"0001-{attempt_id}"
    )
    coverage_record = decode_document(_valid_document(), DurableTargetIdentity(_TARGET_IDENTITY))
    return publish_coverage_record(attempt / "coverage-record", coverage_record).coverage_record


def _reference(origin_target: Path, nested: Path) -> CoverageRecordReference:
    return build_coverage_record_reference(
        simulation_record_id=_SIMULATION_RECORD_ID,
        simulation_manifest_sha256=_MANIFEST_SHA256,
        target_identity=_TARGET_IDENTITY,
        target_selector=_TARGET_SELECTOR,
        origin_invocation_id=_ORIGIN_INVOCATION_ID,
        producer_invocation_id=19,
        simulation_work_item_id=_WORK_ITEM_ID,
        simulation_attempt_id=_SIMULATION_ATTEMPT_ID,
        origin_target_directory=origin_target,
        coverage_record_path=nested,
    )


def _rewrite_reference(
    value: CoverageRecordReference, pointer: tuple[str, ...], replacement: object
) -> bytes:
    document = json.loads(encode_coverage_record_reference(value))
    selected: dict[str, object] = document
    for component in pointer[:-1]:
        selected = selected[component]  # type: ignore[assignment]
    selected[pointer[-1]] = replacement
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )


@pytest.fixture
def published(tmp_path: Path):
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_coverage_record(target)
    return target, nested, _reference(target, nested)


def test_reference_codec_round_trips_exact_canonical_immutable_document(published) -> None:
    _, _, value = published

    raw = encode_coverage_record_reference(value)
    decoded = decode_coverage_record_reference(raw)

    assert encode_coverage_record_reference(decoded) == raw
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    with pytest.raises(TypeError):
        decoded.document["origin_invocation_id"] = 99  # type: ignore[index]
    with pytest.raises(TypeError):
        decoded.document["target"]["identity"] = "substitute"  # type: ignore[index]


def test_building_and_encoding_a_built_reference_never_decodes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reference Booley built from its own values is encoded without re-validation."""
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_coverage_record(target)

    def refuse(_raw: bytes) -> CoverageRecordReference:
        raise AssertionError("a Booley-built reference was decoded")

    monkeypatch.setattr(coverage_reference, "decode_coverage_record_reference", refuse)

    value = _reference(target, nested)

    assert encode_coverage_record_reference(value).endswith(b"\n")


@pytest.mark.parametrize(
    ("pointer", "replacement"),
    [
        (("$schema",), "booley.coverage-record-reference/v2"),
        (("simulation_record_id",), "12345678-1234-1234-9234-123456789abc"),
        (("simulation_manifest_sha256",), "sha256:" + "A" * 64),
        (("origin_invocation_id",), True),
        (("origin_invocation_id",), 0),
        (("producer_invocation_id",), False),
        (("simulation_work_item_id",), "item:1:0123456789abcdef"),
        (("simulation_attempt_id",), "87654321-4321-1432-a234-cba987654321"),
        (("target", "identity"), ""),
        (("coverage_record", "path_base"), "report_root"),
        (("coverage_record", "path"), "/record/coverage.json"),
        (("coverage_record", "path"), "record/../coverage.json"),
        (("coverage_record", "path"), "record/work-items/\0/coverage.json"),
        (("coverage_record", "path"), "elsewhere/coverage.json"),
        (("coverage_record", "bytes"), True),
        (("coverage_record", "bytes"), 0),
        (("coverage_record", "sha256"), "sha256:" + "0" * 63),
        (("coverage_record", "coverage_record_id"), ""),
        (("coverage_record", "schema"), "booley.coverage-record/v2"),
    ],
)
def test_reference_codec_rejects_boundary_and_identity_substitutions(
    published, pointer: tuple[str, ...], replacement: object
) -> None:
    _, _, value = published

    with pytest.raises(CoverageRecordReferenceError):
        decode_coverage_record_reference(_rewrite_reference(value, pointer, replacement))


def test_reference_codec_refuses_a_nested_v4_coverage_record_as_an_older_booley_record(
    published,
) -> None:
    """Booley 0.3.0 wrote v4: it is too old, not an invalid storage contract."""
    _, _, value = published

    with pytest.raises(UnsupportedSimulationRecordFormatError, match="older Booley"):
        decode_coverage_record_reference(
            _rewrite_reference(value, ("coverage_record", "schema"), "booley.coverage-campaign/v4")
        )


def test_resolving_a_nested_v4_reference_outside_the_report_layout_cannot_name_a_directory(
    published,
    tmp_path: Path,
) -> None:
    _, _, value = published
    stray = tmp_path / "stray" / "coverage.json"
    stray.parent.mkdir()
    stray.write_bytes(
        _rewrite_reference(value, ("coverage_record", "schema"), "booley.coverage-campaign/v4")
    )

    with pytest.raises(UnsupportedSimulationRecordFormatError) as excinfo:
        resolve_coverage_record_reference(stray)

    assert "delete its Simulation invocation directory and start a fresh run" in str(excinfo.value)


@pytest.mark.parametrize(
    "raw",
    [b"{}\n", b"{ }\n", b"x" * (MAX_REFERENCE_BYTES + 1)],
    ids=["empty-object", "noncanonical-bytes", "oversize"],
)
def test_reference_codec_rejects_missing_fields_noncanonical_bytes_and_size(raw: bytes) -> None:
    with pytest.raises(CoverageRecordReferenceError):
        decode_coverage_record_reference(raw)


@pytest.mark.parametrize("container", [(), ("target",), ("coverage_record",)])
def test_reference_codec_rejects_unknown_fields(published, container: tuple[str, ...]) -> None:
    _, _, value = published
    document = json.loads(encode_coverage_record_reference(value))
    selected = document if not container else document[container[0]]
    selected["substitute"] = "unexpected"  # type: ignore[index]
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode() + b"\n"

    with pytest.raises(CoverageRecordReferenceError, match="exact fields"):
        decode_coverage_record_reference(raw)


def test_publication_is_create_only_and_idempotent_for_identical_bytes(published) -> None:
    target, _, value = published
    path = target / "coverage.json"

    assert publish_coverage_record_reference(path, value) == value
    recovered = publish_coverage_record_reference(path, value)
    assert encode_coverage_record_reference(recovered) == path.read_bytes()

    conflicting = decode_coverage_record_reference(
        _rewrite_reference(value, ("producer_invocation_id",), 20)
    )
    with pytest.raises(CoverageRecordReferenceError, match="conflicts"):
        publish_coverage_record_reference(path, conflicting)


def test_resolver_authenticates_real_nested_coverage_record_at_exact_attempt_path(
    tmp_path: Path,
) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_coverage_record(target)
    path = target / "coverage.json"
    publish_coverage_record_reference(path, _reference(target, nested))

    resolved = resolve_coverage_record_reference(path)

    assert resolved.coverage_record_path == nested
    assert (
        resolved.loaded.coverage_record.coverage_record_id
        == _valid_document()["coverage_record_id"]
    )
    assert resolved.loaded.coverage_record.target.identity == _TARGET_IDENTITY


def test_resolver_propagates_nested_coverage_record_validation_findings(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    nested = _nested_coverage_record(target)
    reference = _reference(target, nested)
    corrupt_coverage_record_with_duplicate_negative_point(nested)
    nested_bytes = nested.read_bytes()
    reference = decode_coverage_record_reference(
        _rewrite_reference(reference, ("coverage_record", "bytes"), len(nested_bytes))
    )
    reference = decode_coverage_record_reference(
        _rewrite_reference(
            reference,
            ("coverage_record", "sha256"),
            "sha256:" + hashlib.sha256(nested_bytes).hexdigest(),
        )
    )
    path = target / "coverage.json"
    publish_coverage_record_reference(path, reference)

    with pytest.raises(CoverageRecordValidationError) as caught:
        resolve_coverage_record_reference(path)

    message = str(caught.value)
    assert "COV_POINT_ID_DUPLICATE at /points/1/id" in message
    assert "COV_POINT_HIT_NONPOSITIVE at /points/1/hits_by_run/run:reset" in message


def test_resolver_decodes_authenticated_reference_bytes_during_path_swap(
    published, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, _, value = published
    honest = encode_coverage_record_reference(value)
    substitute = _rewrite_reference(value, ("producer_invocation_id",), 20)
    path = target / "coverage.json"
    path.write_bytes(honest)
    original = coverage_reference.decode_coverage_record_reference

    def swap_after_authenticated_read(raw: bytes) -> CoverageRecordReference:
        path.write_bytes(substitute)
        return original(raw)

    monkeypatch.setattr(
        coverage_reference, "decode_coverage_record_reference", swap_after_authenticated_read
    )
    resolved = coverage_reference.resolve_coverage_record_reference(path)

    assert resolved.reference.document["producer_invocation_id"] == 19
    assert path.read_bytes() == substitute


def test_resolver_reads_the_nested_coverage_record_without_rehashing_it(published) -> None:
    target, nested, value = published
    raw = _rewrite_reference(
        value,
        ("coverage_record", "sha256"),
        "sha256:" + hashlib.sha256(b"substitute").hexdigest(),
    )
    path = target / "coverage.json"
    path.write_bytes(raw)

    resolved = resolve_coverage_record_reference(path)

    assert resolved.coverage_record_path == nested
    assert (
        resolved.loaded.coverage_record.coverage_record_id
        == _valid_document()["coverage_record_id"]
    )


def test_resolver_rejects_linked_reference_and_nested_path_components(published) -> None:
    target, _, value = published
    real_reference = target / "real-reference.json"
    real_reference.write_bytes(encode_coverage_record_reference(value))
    (target / "coverage.json").symlink_to(real_reference.name)
    with pytest.raises(CoverageRecordReferenceError, match="not a regular file"):
        resolve_coverage_record_reference(target / "coverage.json")

    (target / "coverage.json").unlink()
    actual_work_items = target / "record" / "work-items"
    moved_work_items = target / "record" / "real-work-items"
    actual_work_items.rename(moved_work_items)
    actual_work_items.symlink_to(moved_work_items.name)
    (target / "coverage.json").write_bytes(encode_coverage_record_reference(value))
    with pytest.raises(CoverageRecordReferenceError, match="contains a link"):
        resolve_coverage_record_reference(target / "coverage.json")


def test_resolver_rejects_attempt_identity_as_unrelated_path_substring(tmp_path: Path) -> None:
    target = tmp_path / "targets" / _TARGET_SELECTOR
    honest = _nested_coverage_record(target)
    misleading = (
        target
        / "record"
        / "work-items"
        / "prefix-0123456789abcdef-suffix"
        / "attempts"
        / f"0001-prefix-{_SIMULATION_ATTEMPT_ID}-suffix"
        / "coverage-record"
        / "coverage.json"
    )
    misleading.parent.mkdir(parents=True)
    misleading.write_bytes(honest.read_bytes())
    (misleading.parent / "coverage-points.jsonl.gz").write_bytes(
        (honest.parent / "coverage-points.jsonl.gz").read_bytes()
    )
    value = _reference(target, misleading)
    path = target / "coverage.json"
    path.write_bytes(encode_coverage_record_reference(value))

    with pytest.raises(CoverageRecordReferenceError, match="Attempt identity"):
        resolve_coverage_record_reference(path)


def test_image_alias_public_references(project_alias):
    alias, target = project_alias
    origin = target / "sim/12/targets/sim_counter"
    nested = _nested_coverage_record(origin)
    reference = _reference(origin, nested)
    public = origin / "coverage.json"
    publish_coverage_record_reference(public, reference)
    resolved = resolve_coverage_record_reference(alias / public.relative_to(target))
    assert resolved.reference_path == public
    assert resolved.coverage_record_path == nested
    (target / "linked").symlink_to(target, target_is_directory=True)
    with pytest.raises((ValueError, OSError), match=r"link|regular"):
        resolve_coverage_record_reference(alias / "linked" / public.relative_to(target))


@pytest.mark.parametrize("linked", ["entry", "below"])
def test_reference_follows_only_the_booley_dir_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, linked: str
) -> None:
    """Build, publish, verify and resolve cross the symlinked ``.booley`` entry only (#2192)."""
    from tests.conftest import symlink_or_skip
    from tests.fusesoc_test_support import symlinked_booley_dir

    monkeypatch.delenv("BOOLEY_DIR", raising=False)
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    physical_reports = symlinked_booley_dir(project, tmp_path / "external-booley") / "flow-reports"
    if linked == "below":
        (tmp_path / "elsewhere").mkdir()
        symlink_or_skip(physical_reports, tmp_path / "elsewhere", target_is_directory=True)
        physical_reports = tmp_path / "elsewhere"
    relative = Path("sim/1/targets") / _TARGET_SELECTOR
    # The nested Coverage Record is published through the physical spelling so only the
    # reference readers see the entry.
    physical_nested = _nested_coverage_record(physical_reports / relative)
    reports = project / ".booley" / "flow-reports"
    target = reports / relative
    nested = target / physical_nested.relative_to(physical_reports / relative)
    public_path = target / "coverage.json"

    if linked == "below":
        physical_value = _reference(physical_reports / relative, physical_nested)
        publish_coverage_record_reference(
            physical_reports / relative / "coverage.json", physical_value
        )
        with pytest.raises(CoverageRecordReferenceError):
            resolve_coverage_record_reference(public_path)
        return
    value = _reference(target, nested)
    assert publish_coverage_record_reference(public_path, value) == value
    assert publish_coverage_record_reference(public_path, value) == value
    assert resolve_coverage_record_reference(public_path).coverage_record_path == nested
