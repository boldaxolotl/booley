"""Model output contract: only hypotheses and advisory suggestions are authored."""


def coverage_analysis_model_schema() -> dict[str, object]:
    """Return a fresh provider-neutral schema for the untrusted model response."""
    references = {"type": "array", "items": {"type": "string"}}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["hypotheses", "recommendations", "waiver_candidates"],
        "properties": {
            "hypotheses": _records({"point_ids": references, "explanation": {"type": "string"}}),
            "recommendations": _records({"point_ids": references, "action": {"type": "string"}}),
            "waiver_candidates": _records(
                {
                    "point_id": {"type": "string"},
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
