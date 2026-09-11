"""Independent QA oracles must reject self-consistent but wrong measurements."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "qa/shared/coverage"
SPEC = importlib.util.spec_from_file_location(
    "qa_measurements", ROOT / "evaluator/measurements.py"
)
oracle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(oracle)


def source_rows(points, rollups):
    return [
        {
            "source": source,
            "rollups": [
                next(
                    (r for r in rollups if r["metric"] == metric),
                    {
                        "metric": metric,
                        "total_points": 0,
                        "eligible_points": 0,
                        "covered_points": 0,
                        "waived_points": 0,
                        "percent": None,
                    },
                )
                for metric in sorted(oracle.SOURCE_METRICS)
            ],
        }
        for source in sorted({p["identity"]["location"]["source"] for p in points})
    ]


def half_campaign():
    points = []
    for bit in range(4):
        for before in range(2):
            label = f"value[{bit}]:{before}->{1 - before}"
            points.append(
                {
                    "id": label,
                    "identity": {
                        "metric": "toggle",
                        "location": {"source": "rtl/toggle.sv", "start": {"line": 2}},
                        "subject": {"signal_bit_direction": label},
                        "collector": {"native_key": label},
                    },
                    "disposition": {"kind": "eligible"},
                    "hits_by_run": {"r1": 1} if bit < 2 else {},
                }
            )
    rollup = {
        "metric": "toggle",
        "total_points": 8,
        "eligible_points": 8,
        "covered_points": 4,
        "waived_points": 0,
        "percent": 50,
    }
    manifest = {
        "target": {"identity": "booley:qa:coverage:1#sim_toggle"},
        "tests": {"runs": [{"id": "r1", "test": "half"}]},
        "rollups": [rollup],
        "source_rollups": source_rows(points, [rollup]),
    }
    case = json.loads((ROOT / "expected.json").read_text())["half"]
    return manifest, points, case


def test_hand_authored_half_has_eight_directional_points():
    manifest, points, case = half_campaign()
    assert oracle.verify_known_answer(manifest, points, case) == {
        "covered": 4,
        "eligible": 8,
        "exact_percent": "50",
    }


def test_same_percentage_with_wrong_denominator_is_rejected():
    manifest, points, case = half_campaign()
    points = points[:2] + points[4:6]
    manifest["rollups"][0].update(total_points=4, eligible_points=4, covered_points=2)
    with pytest.raises(ValueError, match="denominator"):
        oracle.verify_known_answer(manifest, points, case)


def test_same_count_with_wrong_bits_is_rejected():
    manifest, points, case = half_campaign()
    for point in points:
        point["hits_by_run"] = {} if point["hits_by_run"] else {"r1": 1}
    with pytest.raises(ValueError, match="hit set"):
        oracle.verify_known_answer(manifest, points, case)


def test_repetition_does_not_inflate_covered_points():
    manifest, points, case = half_campaign()
    case = json.loads((ROOT / "expected.json").read_text())["repeat"]
    manifest["tests"]["runs"][0]["test"] = "repeat"
    for point in points:
        if point["hits_by_run"]:
            point["hits_by_run"]["r1"] = 3
    assert oracle.verify_known_answer(manifest, points, case)["exact_percent"] == "50"


def test_exact_threshold_above_half_fails_even_when_display_is_half():
    manifest, points, case = half_campaign()
    case["min_pct"] = 50.01
    manifest["evaluation"] = {"status": "fail"}
    oracle.verify_known_answer(manifest, points, case)
    manifest["evaluation"]["status"] = "pass"
    with pytest.raises(ValueError, match="policy verdict"):
        oracle.verify_known_answer(manifest, points, case)


@pytest.mark.parametrize("bad", [0, -1, True])
def test_nonpositive_or_boolean_incidence_rejected(bad):
    _, points, _ = half_campaign()
    points[0]["hits_by_run"]["r1"] = bad
    with pytest.raises(ValueError, match="positive integer"):
        oracle.counts(points)


def test_native_disagreement_is_not_hidden_by_matching_rollups(tmp_path):
    _, points, _ = half_campaign()
    native = tmp_path / "half.dat"
    native.write_text(
        "# SystemC::Coverage-3\n"
        + "".join(f"C '{p['id']}' {2 if p['hits_by_run'] else 0}\n" for p in points)
    )
    with pytest.raises(ValueError, match="native/normalized"):
        oracle.verify_native(points, {"r1": native})


def test_complementary_test_union_is_not_an_average():
    manifest, points, _ = half_campaign()
    case = json.loads((ROOT / "expected.json").read_text())["union"]
    manifest["tests"]["runs"].append({"id": "r2", "test": "upper"})
    for point in points[4:]:
        point["hits_by_run"] = {"r2": 1}
    manifest["rollups"][0].update(covered_points=8, percent=100)
    assert oracle.verify_known_answer(manifest, points, case)["exact_percent"] == "100"
    wrong = copy.deepcopy(manifest)
    wrong["rollups"][0].update(covered_points=4, percent=50)
    with pytest.raises(ValueError, match="rollup count"):
        oracle.verify_known_answer(wrong, points, case)


def test_cover_properties_need_no_source_rollup():
    case = json.loads((ROOT / "expected.json").read_text())["properties4-half"]
    points = [
        {
            "id": str(i),
            "identity": {
                "metric": "cover_property",
                "location": {"source": "rtl/properties4.sv", "start": {"line": i + 2}},
            },
            "disposition": {"kind": "eligible"},
            "hits_by_run": {"r": 1} if i < 2 else {},
        }
        for i in range(4)
    ]
    manifest = {
        "target": {"identity": "booley:qa:coverage:1#sim_properties4"},
        "tests": {"runs": [{"id": "r", "test": "half"}]},
        "source_rollups": source_rows(points, []),
        "rollups": [
            {
                "metric": "cover_property",
                "total_points": 4,
                "eligible_points": 4,
                "covered_points": 2,
                "waived_points": 0,
                "percent": 50,
            }
        ],
    }
    assert oracle.verify_known_answer(manifest, points, case)["exact_percent"] == "50"


@pytest.mark.parametrize("field", ["rollups", "source_rollups"])
def test_omitted_rollup_inventory_is_rejected(field):
    manifest, points, case = half_campaign()
    manifest[field] = []
    with pytest.raises(ValueError, match="inventory"):
        oracle.verify_known_answer(manifest, points, case)


def test_self_consistent_raw_and_normalized_wrong_counts_rejected(tmp_path):
    manifest, points, case = half_campaign()
    points[0]["hits_by_run"]["r1"] = 3
    raw = tmp_path / "half.dat"
    raw.write_text("".join(f"C '{p['id']}' {p['hits_by_run'].get('r1', 0)}\n" for p in points))
    oracle.verify_native(points, {"r1": raw})
    with pytest.raises(ValueError, match="transition/sample count"):
        oracle.verify_known_answer(manifest, points, case)


@pytest.mark.parametrize("hits", [0, 1])
def test_native_record_omitted_from_normalized_inventory_is_rejected(tmp_path, hits):
    _, points, _ = half_campaign()
    raw = tmp_path / "half.dat"
    raw.write_text(
        "".join(f"C '{p['id']}' {p['hits_by_run'].get('r1', 0)}\n" for p in points)
        + f"C 'unrepresented-native-point' {hits}\n"
    )
    with pytest.raises(ValueError, match="point inventory"):
        oracle.verify_native(points, {"r1": raw})
