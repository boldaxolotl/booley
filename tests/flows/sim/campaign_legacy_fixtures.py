"""Released campaign inputs for read-only compatibility and retention tests."""

import json

from booley.flows.sim.campaign.codec import encode_simulation_campaign_manifest
from booley.flows.sim.campaign.flow_planning import _variant_document, _work_items
from booley.flows.sim.campaign.planning import finalize_manifest
from booley.flows.test_runs import build_test_run


def _legacy_manifest(schema="v1", *, target=None):
    from tests.flows.sim.test_campaign_manifest_codec import _manifest, _names_only_work_items

    assert schema in {"v1", "v2"}
    document = _manifest()
    document.pop("fingerprints")
    if target is not None:
        document["target"] = target
    document["$schema"] = "booley.simulation-campaign-manifest/" + schema
    workload = document["workload"]
    if schema == "v2":
        workload.update(coverage=True, no_waivers=True)
    variant = _variant_document(workload, [], False)
    document["build_variants"] = [variant]
    document["work_items"] = _names_only_work_items(
        _work_items(
            [(build_test_run(None),)],
            document["target"],
            variant,
            "run",
            "literal",
            kind="ordinary_hdl",
        )
    )
    return json.loads(encode_simulation_campaign_manifest(finalize_manifest(document)))
