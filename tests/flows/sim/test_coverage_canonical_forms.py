"""Golden bytes for every canonical coverage form that a stored digest depends on.

Each expected value below is a literal produced once by the code that wrote it
before the shared JSON helper set existed. Never fix a failure here by
regenerating a literal: the failure means a stored form changed, and only an
intentional change of that stored form may update its literal.

Every input holds unsorted keys and non-ASCII text in a field the pinned form
keeps, so key sorting and ``ensure_ascii`` are both observable. All pinned inputs
are literals except the campaign of the point-store and manifest tests, which
is the shared valid Campaign document with literal overrides.
"""

from __future__ import annotations

import gzip
import hashlib
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType

from booley.flows.sim.coverage_campaign import (
    DurableTargetIdentity,
    decode_coverage_campaign,
    decode_coverage_point_id,
)
from booley.flows.sim.coverage_campaign_store import (
    load_coverage_campaign,
    publish_coverage_campaign,
)
from booley.flows.sim.coverage_policy import (
    ApprovedWaiverSet,
    CoverageCriterion,
    CoverageThreshold,
    evaluate_coverage_campaign,
)
from booley.flows.sim.coverage_provenance import coverage_digest
from booley.flows.sim.coverage_reference import (
    CoverageCampaignReference,
    encode_coverage_campaign_reference,
)
from booley.flows.sim.coverage_waivers import (
    CoverageRepositoryRoots,
    CoverageWaiverConfig,
    load_approved_waiver_set,
)
from booley.flows.sim.verilator_coverage import (
    CoverageCollectionRequest,
    CoverageSource,
    CoverageTarget,
    SelectedCoverageRun,
    collect,
)
from tests.flows.sim.test_coverage_campaign import _valid_document
from tests.flows.sim.test_coverage_policy import _TARGET, _two_metric_campaign
from tests.flows.sim.test_verilator_coverage_collector import _HEADER, _PresenceExecution

GOLDEN_POINT_ID = (
    "cp1:eyJjb2xsZWN0b3IiOnsibmF0aXZlX2tleSI6Ilx1MDAwMWZcdTAwMDJydGwvY291bnRlci5zdlx1MDAwMWxc"
    "dTAwMDIxMFx1MDAwMW5cdTAwMDIzXHUwMDAxaFx1MDAwMlRPUC56w6RobGVyXHUwMDAxdFx1MDAwMmxpbmVcdTAw"
    "MDFvXHUwMDAyYmxvY2siLCJyZWNvcmRfdHlwZSI6ImxpbmUifSwiaGllcmFyY2h5IjoiVE9QLnrDpGhsZXIiLCJs"
    "b2NhdGlvbiI6eyJlbmQiOnsiY29sdW1uIjozLCJsaW5lIjoxMH0sInNvdXJjZSI6InJ0bC9jb3VudGVyLnN2Iiwi"
    "c3RhcnQiOnsiY29sdW1uIjozLCJsaW5lIjoxMH19LCJtZXRyaWMiOiJsaW5lIiwic3ViamVjdCI6eyJiYXNpY19i"
    "bG9jayI6ImJsb2NrIn19"
)
# Keys are deliberately unsorted: the encoders must sort them.
GOLDEN_IDENTITY = {
    "subject": {"basic_block": "block"},
    "metric": "line",
    "location": {
        "start": {"line": 10, "column": 3},
        "source": "rtl/counter.sv",
        "end": {"line": 10, "column": 3},
    },
    "hierarchy": "TOP.zähler",
    "collector": {
        "record_type": "line",
        "native_key": "\x01f\x02rtl/counter.sv\x01l\x0210\x01n\x023\x01h\x02TOP.zähler\x01t\x02line\x01o\x02block",
    },
}
GOLDEN_REFERENCE_BYTES = (
    b'{"$schema":"booley.coverage-campaign-reference/v1","coverage_campaign":{"bytes":5386,'
    b'"campaign_id":"campaign:sim_counter:2026-09-01T10:30:00Z","path":"campaign/work-items/'
    b"0001-0123456789abcdef/attempts/0001-87654321-4321-4432-a234-cba987654321/"
    b'coverage-campaign/coverage.json","path_base":"origin_target",'
    b'"schema":"booley.coverage-campaign/v5","sha256":"sha256:' + b"b" * 64 + b'"},'
    b'"origin_invocation_id":12,"producer_invocation_id":19,'
    b'"simulation_attempt_id":"87654321-4321-4432-a234-cba987654321",'
    b'"simulation_campaign_id":"12345678-1234-4234-9234-123456789abc",'
    b'"simulation_manifest_sha256":"sha256:' + b"a" * 64 + b'",'
    b'"simulation_work_item_id":"item:0000:0123456789abcdef",'
    b'"target":{"identity":"acme:demo:counter:1.0#sim_counter","selector":"sim_z\xc3\xa4hler"}}\n'
)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def test_point_id_golden_is_shared_by_collector_and_codec(tmp_path: Path) -> None:
    record = (
        "C '\x01f\x02rtl/counter.sv\x01l\x0210\x01n\x023\x01h\x02TOP.zähler"
        "\x01t\x02line\x01o\x02block' 2\n"
    )
    request = CoverageCollectionRequest(
        target=CoverageTarget(
            identity="acme:demo:counter:1.0#sim_counter",
            selector="sim_counter",
            toplevel="counter",
            harness="generated_main",
            sources=(CoverageSource("rtl/counter.sv", "rtl/counter.sv", "rtl"),),
            custom_main_hooks=(),
        ),
        selected_tests=(SelectedCoverageRun("reset"),),
        artifact_root=tmp_path / "campaign",
        reset_included=True,
    )

    result = collect(request, _PresenceExecution(_HEADER + record))

    point = result.points[0]
    assert point.id == GOLDEN_POINT_ID
    assert decode_coverage_point_id(GOLDEN_POINT_ID) == GOLDEN_IDENTITY


def _campaign_document() -> dict[str, object]:
    """The shared valid Campaign with the golden point and a non-ASCII selector."""
    document = _valid_document()
    document["target"]["selector"] = "sim_zähler"  # type: ignore[index]
    document["points"][0].update(id=GOLDEN_POINT_ID, identity=GOLDEN_IDENTITY)  # type: ignore[index]
    return document


def _publish(tmp_path: Path):
    campaign = decode_coverage_campaign(
        _campaign_document(), DurableTargetIdentity("acme:demo:counter:1.0#sim_counter")
    )
    return publish_coverage_campaign(tmp_path / "campaign", campaign)


def test_point_store_lines_golden(tmp_path: Path) -> None:
    paths = _publish(tmp_path)

    lines = gzip.decompress(paths.points.read_bytes())

    assert "zähler".encode() in lines
    assert lines.endswith(b"\n")
    assert _sha256_hex(lines) == "609dfae9ad04d68120e098360b3891688bd71492ff80276907a92dd18a709c4c"


def test_manifest_golden(tmp_path: Path) -> None:
    paths = _publish(tmp_path)
    raw = paths.campaign.read_bytes()
    compressed = paths.points.read_bytes()

    # zlib builds differ in compressed bytes, so only these two values are normalized.
    normalized = raw.replace(_sha256_hex(compressed).encode(), b"<gz-sha256>")
    assert normalized != raw
    length_text = f'"bytes": {len(compressed)},\n    "encoding": "jsonl+gzip"'.encode()
    assert normalized.count(length_text) == 1
    normalized = normalized.replace(
        length_text, b'"bytes": <gz-length>,\n    "encoding": "jsonl+gzip"'
    )

    assert b"\\u00e4" in raw
    assert "ä".encode() not in raw
    assert (
        _sha256_hex(normalized)
        == "cc701b79374df1d0f6266b77021345ed6d07eb0863093ce428f223db55872e41"
    )
    loaded = load_coverage_campaign(paths.campaign)
    assert loaded.summary.manifest_sha256 == f"sha256:{_sha256_hex(raw)}"


def test_coverage_campaign_reference_golden() -> None:
    # Keys are deliberately unsorted: the encoder must sort them.
    reference = CoverageCampaignReference(
        {
            "target": {"selector": "sim_zähler", "identity": "acme:demo:counter:1.0#sim_counter"},
            "simulation_work_item_id": "item:0000:0123456789abcdef",
            "simulation_manifest_sha256": "sha256:" + "a" * 64,
            "simulation_campaign_id": "12345678-1234-4234-9234-123456789abc",
            "simulation_attempt_id": "87654321-4321-4432-a234-cba987654321",
            "producer_invocation_id": 19,
            "origin_invocation_id": 12,
            "coverage_campaign": {
                "sha256": "sha256:" + "b" * 64,
                "schema": "booley.coverage-campaign/v5",
                "path_base": "origin_target",
                "path": "campaign/work-items/0001-0123456789abcdef/attempts/0001-87654321-4321-4432-a234-cba987654321/coverage-campaign/coverage.json",
                "campaign_id": "campaign:sim_counter:2026-09-01T10:30:00Z",
                "bytes": 5386,
            },
            "$schema": "booley.coverage-campaign-reference/v1",
        }
    )

    raw = encode_coverage_campaign_reference(reference)

    assert raw == GOLDEN_REFERENCE_BYTES
    assert _sha256_hex(raw) == "66a1c0eb6e5853ea3285d31156d722a77a65992e1b1ee048e4a8a3a64dc83add"


def test_approved_waiver_set_digest_golden(tmp_path: Path) -> None:
    rtl = tmp_path / "rtl-repository"
    project_data = tmp_path / "project-data-repository"
    source = rtl / "rtl" / "counter.sv"
    source_bytes = b"module counter; endmodule\n"
    waiver_file = project_data / "coverage-waivers" / "rtl" / "counter.sv.toml"
    source.parent.mkdir(parents=True)
    waiver_file.parent.mkdir(parents=True)
    source.write_bytes(source_bytes)
    waiver_file.write_bytes(
        f'''schema = "booley.coverage-waivers/v1"
source = "rtl/counter.sv"
source_sha256 = "sha256:{_sha256_hex(source_bytes)}"

[[approval]]
id = "counter-basic-block"
target = "acme:demo:counter:1.0#sim_counter"
point_id = "{GOLDEN_POINT_ID}"
reason = "excluded"
justification = "Reserved behavior of zähler is intentionally excluded."
approved_by = "verification-owner@example.test"
approved_at = "2026-08-31T09:00:00Z"
approval_ref = "review:CR-1042"
'''.encode()
    )
    roots = CoverageRepositoryRoots(rtl_repository=rtl, project_data_repository=project_data)
    config = CoverageWaiverConfig(anchor="project_data_repository", directory="coverage-waivers")

    approved = load_approved_waiver_set(config, roots, known_targets=(_TARGET,))

    assert (
        approved.digest
        == "sha256:a97ff6046fe5ab5a68d099fd37481063c18cbf725fad536269ed3fcd875fa4b1"
    )
    assert (
        load_approved_waiver_set(None, roots, known_targets=()).digest
        == "sha256:ae024874fd9c68b49bff42973ac809b623666de39afb78cb51d6a01e901ca5f3"
    )


def test_criterion_fingerprint_golden() -> None:
    criterion = CoverageCriterion(
        target=_TARGET,
        thresholds=(
            CoverageThreshold(metric="branch", minimum_percent=Fraction("62.5")),
            CoverageThreshold(metric="line", minimum_percent=Fraction(50)),
        ),
        tests=("smoke",),
    )
    waivers = ApprovedWaiverSet(
        configuration=MappingProxyType(
            {"directory": "waivers-zähler", "anchor": "project_data_repository"}
        ),
        digest="sha256:" + "c" * 64,
        waivers=(),
    )

    evaluated = evaluate_coverage_campaign(_two_metric_campaign(), criterion, waivers)

    assert (
        evaluated.evaluation["criterion_fingerprint"]
        == "sha256:e50bfbdd4a189ccf1f4872a2b02831a97e59adefc9ed6755141fcba21c025612"
    )


def test_coverage_campaign_fingerprint_golden() -> None:
    value = {
        "zähler": MappingProxyType({"b": (1, 2.5), "a": None}),
        "items": ("x", {"y": True, "ä": "ö"}),
    }

    assert (
        coverage_digest(value)
        == "sha256:caad4a65256f4c3c55e31e7b9ac62a4470561c69f522a0928356d84af7be4bf2"
    )
