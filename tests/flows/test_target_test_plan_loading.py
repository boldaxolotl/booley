"""The Test Run plan of a Target reads the section of its own core."""

from pathlib import Path

from booley.flows.target_test_plan_loading import load_checkout_target_test_plan
from tests.target_test_support import write_two_core_sim_project


def test_checkout_plan_uses_the_selected_cores_section(tmp_path: Path):
    write_two_core_sim_project(
        tmp_path,
        '["vendor:a:x#sim"]\ntests = ["a_test"]\n["vendor:b:y#sim"]\ntests = ["b_test"]\n',
    )
    assert load_checkout_target_test_plan(tmp_path, "x#sim").tests == ("a_test",)
    assert load_checkout_target_test_plan(tmp_path, "y#sim").tests == ("b_test",)
