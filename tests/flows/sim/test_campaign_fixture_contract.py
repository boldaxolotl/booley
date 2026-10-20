"""Executing campaign fixtures carry the current Test Run identity."""

import json

from booley.flows.sim.campaign.codec import (
    MANIFEST_SCHEMA,
    encode_simulation_campaign_manifest,
)
from booley.flows.test_runs import build_test_run, test_run_document


def test_shared_manifest_has_current_test_runs():
    from tests.flows.sim.test_campaign_manifest_codec import _manifest
    from tests.flows.sim.test_campaign_phase3 import (
        _named_manifest,
        _single_item_selection_manifest,
        _two_item_manifest,
        _unfiltered_cocotb_manifest,
    )
    from tests.flows.sim.test_campaign_phase3_integrity import _manifest_for

    manifests = (
        _manifest(),
        _named_manifest(("alpha",)),
        _two_item_manifest(),
        _unfiltered_cocotb_manifest(),
        _single_item_selection_manifest("named", ("alpha", "beta")),
        _single_item_selection_manifest("default", ()),
        _manifest_for(("alpha", "beta")),
    )
    for manifest in manifests:
        document = (
            manifest
            if isinstance(manifest, dict)
            else json.loads(encode_simulation_campaign_manifest(manifest))
        )
        assert document["$schema"] == MANIFEST_SCHEMA, "shared fixture must use current manifest"
        for item in document["work_items"]:
            assert item["test_runs"] == [
                test_run_document(build_test_run(name))
                for name in item["selection"]["names"] or (None,)
            ]
