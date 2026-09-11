"""Deterministic heterogeneous Coverage Campaigns for scale characterization."""

from dataclasses import replace

from booley.flows.sim.coverage_campaign import (
    CoverageCapability,
    CoveragePoint,
    CoveragePointIdentity,
    DurableTargetIdentity,
    _point_id,
    decode_coverage_campaign,
    derive_coverage_rollups,
    encode_coverage_point,
    freeze_coverage_mapping,
)
from tests.flows.sim.test_coverage_campaign import _valid_document


def scale_campaign(point_count: int):
    """Build a stable mixed Campaign whose point population has realistic query dimensions."""
    document = _valid_document()
    target = DurableTargetIdentity(document["target"]["identity"])
    base = decode_coverage_campaign(document, target)
    attributes = base.collector.capabilities[0].attributes
    capabilities = tuple(
        CoverageCapability(metric, "reported", attributes)
        for metric in ("line", "branch", "expression", "toggle")
    )
    collector = replace(base.collector, capabilities=capabilities)
    points = tuple(_scale_point(index) for index in range(point_count))
    return replace(
        base,
        collector=collector,
        points=points,
        rollups=derive_coverage_rollups(points),
    )


def _scale_point(index: int) -> CoveragePoint:
    metric = ("line", "branch", "expression", "toggle")[index % 4]
    identity = CoveragePointIdentity(
        metric=metric,
        location=freeze_coverage_mapping(
            {
                "source": "rtl/counter.sv",
                "start": {"line": index + 1, "column": 1},
                "end": {"line": index + 1, "column": 2},
            }
        ),
        hierarchy=f"TOP.counter.block_{index % 64}",
        subject=freeze_coverage_mapping({"outcome": index}),
        collector=freeze_coverage_mapping(
            {"record_type": f"v_{metric}", "native_key": f"{metric}:{index}"}
        ),
    )
    disposition = (
        {
            "kind": "waived",
            "reason": "unreachable",
            "waiver_id": f"waiver:scale-{index}",
            "waiver_file": "coverage/waivers.toml",
            "waiver_fingerprint": "sha256:" + "8" * 64,
            "provenance": {
                "justification": "Deterministic characterization fixture",
                "approved_by": "fixture",
                "approved_at": "2026-09-10T08:00:00Z",
                "approval_ref": f"fixture:{index}",
                "proof": {"kind": "formal", "reference": f"proof:{index}"},
            },
        }
        if index % 17 == 0
        else {"kind": "eligible"}
    )
    point = CoveragePoint(
        "pending",
        identity,
        {} if index % 3 == 0 else {"run:reset": (index % 7) + 1},
        freeze_coverage_mapping(disposition),
    )
    encoded_identity = encode_coverage_point(point)["identity"]
    return replace(point, id=_point_id(encoded_identity))
