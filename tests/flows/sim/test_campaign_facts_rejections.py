from __future__ import annotations

import pytest

from booley.evidence.fields import ACCEPTANCE_FACTS_SCHEMA_V1
from booley.flows.sim.campaign import facts
from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError, canonical_json_bytes


def _facts() -> dict[str, object]:
    return {
        "$schema": ACCEPTANCE_FACTS_SCHEMA_V1,
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "manifest_sha256": "sha256:" + "1" * 64,
        "origin": {"execution_id": "e" * 32, "invocation_id": 1},
        "target": {
            "vlnv": "acme:lib:dut:1",
            "name": "sim",
            "selector": "sim",
            "project_identity": "project",
            "revision": "abc",
            "role": "candidate",
            "display_name": "sim",
        },
        "required_suite": {
            "names": [],
            "default_invocation": True,
            "source_sha256": "sha256:" + "2" * 64,
        },
        "prerequisites": [],
        "consumed_results": [],
        "observations": [],
        "coverage_reference": None,
    }


def test_acceptance_facts_reject_noncanonical_wrong_shape_and_schema() -> None:
    facts.decode_acceptance_facts(canonical_json_bytes(_facts()))
    missing = _facts()
    del missing["campaign_id"]
    with pytest.raises(SimulationCampaignIntegrityError, match="valid JSON"):
        facts.decode_acceptance_facts(b"{")
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts.decode_acceptance_facts(canonical_json_bytes(missing))
    with pytest.raises(SimulationCampaignIntegrityError, match="not canonical"):
        facts.decode_acceptance_facts(canonical_json_bytes(_facts()).rstrip())
    value = _facts()
    value["$schema"] = "unknown"
    with pytest.raises(SimulationCampaignIntegrityError, match="unsupported"):
        facts.decode_acceptance_facts(canonical_json_bytes(value))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("required_suite", "names"), ["a", "a"]),
        (("required_suite", "default_invocation"), 1),
        (("required_suite", "names"), ["named"]),
        (("manifest_sha256",), "bad"),
        (("campaign_id",), 1),
        (("campaign_id",), "bad"),
        (("campaign_id",), "550e8400-e29b-11d4-a716-446655440000"),
    ],
)
def test_acceptance_facts_reject_invalid_identity_and_suite(
    path: tuple[str | int, ...], value: object
) -> None:
    document = _facts()
    target: object = document
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


def _observation() -> dict[str, object]:
    return {
        "work_item_id": "item:0000:0123456789abcdef",
        "role": "candidate",
        "revision": "abc",
        "target": "sim",
        "result_sha256": "sha256:" + "3" * 64,
        "test": "smoke",
        "execution": "completed",
        "failure_class": None,
        "functional": "pass",
        "assertions": "clean",
        "assertion_count": 0,
        "detail": {},
        "cycle_count": None,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test", 1),
        ("detail", {"x": "y" * 3000}),
        ("execution", "unknown"),
        ("failure_class", "unknown"),
        ("functional", "unknown"),
        ("assertions", "unknown"),
        ("assertion_count", -1),
        ("cycle_count", -1),
    ],
)
def test_acceptance_observation_rejects_invalid_values(field: str, value: object) -> None:
    observation = _observation()
    observation[field] = value
    with pytest.raises(SimulationCampaignIntegrityError):
        facts._validate_observation(observation, 0)


@pytest.mark.parametrize("tests", [("smoke", "smoke"), (None, "default")])
def test_acceptance_facts_reject_duplicate_observation_tests(
    tests: tuple[str | None, str | None],
) -> None:
    document = _facts()
    first = _observation()
    second = _observation()
    first["test"], second["test"] = tests
    document["observations"] = [first, second]

    with pytest.raises(SimulationCampaignIntegrityError, match="repeat an observation test"):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("path_base", "campaign"),
        ("path", 1),
        ("bytes", -1),
        ("sha256", "bad"),
    ],
)
def test_acceptance_evidence_rejects_invalid_values(field: str, value: object) -> None:
    evidence = {
        "path_base": "origin_invocation",
        "path": "result.json",
        "bytes": 1,
        "sha256": "sha256:" + "4" * 64,
        "kind": "simulation_result",
        "owner": "owner",
    }
    evidence[field] = value
    with pytest.raises(SimulationCampaignIntegrityError):
        facts._evidence(evidence, "evidence")


def test_acceptance_fact_collection_helpers_are_bounded_and_exact() -> None:
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts._exact([], set(), "value")
    with pytest.raises(SimulationCampaignIntegrityError, match="bounded list"):
        facts._list("not-list", "value", 1)
    with pytest.raises(SimulationCampaignIntegrityError, match="sha256"):
        facts._digest("sha256:" + "z" * 64, "digest")


def test_acceptance_fact_size_type_and_prerequisite_guards(monkeypatch) -> None:
    monkeypatch.setattr(facts, "_MAX_BYTES", 0)
    with pytest.raises(SimulationCampaignIntegrityError, match="size ceiling"):
        facts.decode_acceptance_facts(b"{}\n")
    with pytest.raises(SimulationCampaignIntegrityError, match="expected"):
        facts.encode_acceptance_facts(object())  # type: ignore[arg-type]

    prerequisite = {
        "role": "candidate",
        "manifest": {},
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "target": {},
        "work_item_id": "item",
        "result": {},
        "cycle_observation": {},
    }
    with pytest.raises(SimulationCampaignIntegrityError, match="role"):
        facts._validate_prerequisite(prerequisite, 0)

    prerequisite["role"] = "cycle_count_baseline"
    evidence = {
        "path_base": "origin_invocation",
        "path": "record.json",
        "bytes": 1,
        "sha256": "sha256:" + "1" * 64,
        "kind": "record",
        "owner": "owner",
    }
    prerequisite["manifest"] = evidence
    prerequisite["result"] = evidence
    prerequisite["cycle_observation"] = {"test": "smoke", "cycle_count": -1, "unit": "cycles"}
    with pytest.raises(SimulationCampaignIntegrityError, match="cycle observation"):
        facts._validate_prerequisite(prerequisite, 0)


def _v2_facts(*test_runs: tuple[str | None, object]) -> dict[str, object]:
    """v2 facts with one observation per ``(test, test_run)`` pair."""
    document = _facts()
    document["$schema"] = facts.ACCEPTANCE_FACTS_SCHEMA
    observations = []
    for test, test_run in test_runs:
        observation = _observation()
        observation["test"] = test
        observation["test_run"] = test_run
        observation["test_run_fingerprint"] = "sha256:" + "9" * 64
        observations.append(observation)
    document["observations"] = observations
    return document


def test_v2_facts_hold_several_test_runs_of_one_test() -> None:
    document = _v2_facts(
        ("stress", "stress+short@1"), ("stress", "stress+soak@1"), ("stress", "stress+soak@7")
    )
    decoded = facts.decode_acceptance_facts(canonical_json_bytes(document))
    assert [item["test_run"] for item in decoded.document["observations"]] == [
        "stress+short@1",
        "stress+soak@1",
        "stress+soak@7",
    ]
    assert facts.encode_acceptance_facts(decoded) == canonical_json_bytes(document)


def test_v2_facts_reject_a_repeated_test_run() -> None:
    document = _v2_facts(("stress", "stress@7"), ("stress", "stress@7"))
    with pytest.raises(SimulationCampaignIntegrityError, match="repeat an observation Test Run"):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


@pytest.mark.parametrize("test_run", [None, "", 7, "x" * 1025])
def test_v2_facts_reject_an_invalid_test_run(test_run: object) -> None:
    document = _v2_facts(("stress", test_run))
    with pytest.raises(SimulationCampaignIntegrityError, match="test_run is invalid"):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


def test_v2_facts_require_the_test_run_of_every_observation() -> None:
    document = _v2_facts(("stress", "stress@7"))
    del document["observations"][0]["test_run"]  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


def test_v1_facts_still_decode_and_may_not_name_test_runs() -> None:
    document = _facts()
    first = _observation()
    first["test"] = "smoke"
    document["observations"] = [first]
    decoded = facts.decode_acceptance_facts(canonical_json_bytes(document))
    assert decoded.document["$schema"] == "booley.simulation-acceptance-facts/v1"
    assert "test_run" not in decoded.document["observations"][0]

    first["test_run"] = "smoke@1"
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


@pytest.mark.parametrize("fingerprint", [None, "", "sha256:short", "9" * 64])
def test_v2_facts_reject_an_invalid_test_run_fingerprint(fingerprint: object) -> None:
    document = _v2_facts(("stress", "stress@7"))
    document["observations"][0]["test_run_fingerprint"] = fingerprint  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


def test_v2_facts_require_the_fingerprint_of_every_test_run() -> None:
    document = _v2_facts(("stress", "stress@7"))
    del document["observations"][0]["test_run_fingerprint"]  # type: ignore[index]
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts.decode_acceptance_facts(canonical_json_bytes(document))


def _prerequisite(cycle: dict[str, object]) -> dict[str, object]:
    evidence = {
        "path_base": "origin_invocation",
        "path": "record.json",
        "bytes": 1,
        "sha256": "sha256:" + "1" * 64,
        "kind": "record",
        "owner": "owner",
    }
    return {
        "role": "cycle_count_baseline",
        "manifest": evidence,
        "campaign_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "target": {},
        "work_item_id": "item",
        "result": evidence,
        "cycle_observation": cycle,
    }


_CYCLE = {"test": "stress", "cycle_count": 9, "unit": "cycles"}
_NAMED_CYCLE = {
    **_CYCLE,
    "test_run": "stress+soak@7",
    "test_run_fingerprint": "sha256:" + "9" * 64,
}


def test_v2_prerequisite_names_the_test_run_the_baseline_ran() -> None:
    """Facts show that both revisions ran the same Test Run, not only one Test."""
    facts._validate_prerequisite(_prerequisite(dict(_NAMED_CYCLE)), 0, names_test_runs=True)
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts._validate_prerequisite(_prerequisite(dict(_CYCLE)), 0, names_test_runs=True)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("test_run", ""),
        ("test_run", 7),
        ("test_run", "x" * 1025),
        ("test_run_fingerprint", "sha256:short"),
        ("test_run_fingerprint", None),
    ],
)
def test_v2_prerequisite_rejects_an_invalid_baseline_test_run(field: str, value: object) -> None:
    cycle = {**_NAMED_CYCLE, field: value}
    with pytest.raises(SimulationCampaignIntegrityError):
        facts._validate_prerequisite(_prerequisite(cycle), 0, names_test_runs=True)


def test_v1_prerequisite_may_not_name_a_test_run() -> None:
    facts._validate_prerequisite(_prerequisite(dict(_CYCLE)), 0)
    with pytest.raises(SimulationCampaignIntegrityError, match="exact fields"):
        facts._validate_prerequisite(_prerequisite(dict(_NAMED_CYCLE)), 0)
