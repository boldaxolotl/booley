"""Guards that replaced decode-time validation of manifest paths and baselines (#2136)."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.flows.sim.campaign.codec import SimulationCampaignIntegrityError, linked_manifest_path
from booley.flows.sim.campaign.flow_planning import plan_ordinary_hdl_campaign
from booley.flows.sim.config import parse_run_cwd_template
from tests.flows.sim.test_campaign_flow_planning import _planning_fixture


@pytest.mark.parametrize(
    "path",
    [
        "targets/x/campaign/manifest.json",
        "targets/x%40baseline-abc/campaign/manifest.json",
    ],
)
def test_a_planned_prerequisite_path_stays_inside_its_invocation(
    tmp_path: Path, path: str
) -> None:
    assert linked_manifest_path(tmp_path, {"path": path}) == tmp_path / path


@pytest.mark.parametrize(
    "path",
    [
        "../outside/targets/x/campaign/manifest.json",
        "targets/../campaign/manifest.json",
        "targets/x/campaign/manifest.json/extra",
        "/abs/targets/x/campaign/manifest.json",
        None,
    ],
)
def test_a_prerequisite_path_that_is_not_a_contained_manifest_is_refused(
    tmp_path: Path, path: object
) -> None:
    with pytest.raises(SimulationCampaignIntegrityError):
        linked_manifest_path(tmp_path, {"path": path})


def test_planning_refuses_a_prerequisite_not_planned_as_a_baseline(tmp_path: Path) -> None:
    """A Cocotb baseline is planned as a candidate, so its link would point nowhere."""
    _project, handle, inspection, preview = _planning_fixture(tmp_path)
    prerequisite = {"target": {"selector": "base", "role": "candidate"}}

    with pytest.raises(
        SimulationCampaignIntegrityError, match=r"'base' was not planned as a Cycle Count baseline"
    ):
        plan_ordinary_hdl_campaign(
            handle=handle,
            inspection=inspection,
            preview=preview,
            groups=preview.groups,
            required_suite=("reset", "count"),
            revision="abc123",
            invocation_id=1,
            execution_id="",
            trace=False,
            prerequisite_documents=(prerequisite,),
        )


@pytest.mark.parametrize("template", ["../shared", "a/../b", "..\\shared", "a\\..\\b"])
def test_run_cwd_refuses_a_parent_component_with_either_separator(template: str) -> None:
    with pytest.raises(ValueError, match=r"must not contain a '\.\.' component"):
        parse_run_cwd_template(template)
