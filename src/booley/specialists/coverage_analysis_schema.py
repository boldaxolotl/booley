"""Model output contract: only hypotheses and advisory suggestions are authored."""

from booley.flows.sim.coverage_evidence import COVERAGE_POINT_REFERENCE_PATTERN


def coverage_analysis_model_schema() -> dict[str, object]:
    """Return a fresh provider-neutral schema for the untrusted model response."""
    reference = {"type": "string", "pattern": COVERAGE_POINT_REFERENCE_PATTERN}
    references = {"type": "array", "items": reference}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["hypotheses", "recommendations", "waiver_candidates"],
        "properties": {
            "hypotheses": _records({"point_refs": references, "explanation": {"type": "string"}}),
            "recommendations": _records({"point_refs": references, "action": {"type": "string"}}),
            "waiver_candidates": _records(
                {
                    "point_ref": reference,
                    "reason": {"type": "string"},
                    "evidence": {"type": "string"},
                    "proof_reference": {"type": "string"},
                }
            ),
        },
    }


def _records(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "array",
        "items": {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    }
