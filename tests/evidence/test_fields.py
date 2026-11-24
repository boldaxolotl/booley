from booley.evidence import fields


def test_persisted_field_names_remain_stable() -> None:
    assert {
        "recipe_param": fields.RECIPE_FINGERPRINT_PARAM,
        "recipe_detail": fields.RECIPE_FINGERPRINT_DETAIL,
        "snapshot_param": fields.RECIPE_SNAPSHOT_PARAM,
        "snapshot_detail": fields.RECIPE_SNAPSHOT_DETAIL,
        "baseline_recipe": fields.BASELINE_RECIPE_FINGERPRINT_DETAIL,
        "baseline_snapshot": fields.BASELINE_RECIPE_SNAPSHOT_DETAIL,
        "baseline_ref_param": fields.BASELINE_REF_PARAM,
        "baseline_ref_detail": fields.BASELINE_REF_DETAIL,
    } == {
        "recipe_param": "_recipe_fingerprint",
        "recipe_detail": "_recipe_fingerprint",
        "snapshot_param": "_recipe_snapshot",
        "snapshot_detail": "_recipe_snapshot",
        "baseline_recipe": "_baseline_recipe_fingerprint",
        "baseline_snapshot": "_baseline_recipe_snapshot",
        "baseline_ref_param": "_baseline_ref",
        "baseline_ref_detail": "_baseline_ref",
    }
