"""No source or test path names the Simulation Record concept "campaign".

The Simulation Record's packages, modules, test modules, test support helpers and fixture
directories use Record names. A path may still say ``campaign`` when it is another concept
(listed below with the reason) or when it reproduces an older on-disk record.
"""

from __future__ import annotations

from pathlib import Path

from tests.source_checkout_support import source_checkout_files

ROOT = Path(__file__).resolve().parents[2]

# Fixture trees that reproduce 0.3.0/0.4.0 on-disk records. Only path components *below*
# these roots are exempt: the root's own name is still checked.
_OLD_RECORD_FIXTURE_ROOTS = (
    "tests/flows/fixtures/booley_0_3_0",
    "tests/flows/fixtures/booley_0_4_0",
    "tests/runtime/fixtures/directory_migration/taxi_simulation_record",
)

_ALLOWED_PATHS = frozenset(
    {
        # Coverage Record modules, tests and support: a later part of the Record rename moves them.
        "src/booley/flows/sim/coverage_campaign.py",
        "src/booley/flows/sim/coverage_campaign_store.py",
        "tests/flows/sim/coverage_campaign_test_support.py",
        "tests/flows/sim/test_coverage_campaign.py",
        "tests/flows/sim/test_coverage_campaign_reference.py",
        "tests/flows/sim/test_coverage_campaign_store.py",
        "tests/qa/test_coverage_campaign_faults.py",
        # Target (Mutation) campaigns keep their name.
        "src/booley/flows/target_campaign.py",
        "tests/flows/test_target_campaign.py",
        "tests/mcp_tools/test_target_campaign_aggregation.py",
        "tests/criteria/test_campaign_templates.py",
        # Goal freshness "campaign" category (#3045).
        "tests/flows/test_source_fingerprint_campaign.py",
    }
)


def _says_old_term(relative: str) -> bool:
    """Whether a path has a component containing "campaign", ignoring old-record fixture interiors."""
    for root in _OLD_RECORD_FIXTURE_ROOTS:
        if relative.startswith(root + "/"):
            # Check the root's own components only; everything below it is an old on-disk name.
            relative = root
            break
    return any("campaign" in part.lower() for part in relative.split("/"))


def test_simulation_record_paths_use_record_names() -> None:
    offenders = [
        relative
        for path in source_checkout_files(ROOT, "src", "tests")
        if (relative := path.relative_to(ROOT).as_posix()) not in _ALLOWED_PATHS
        and _says_old_term(relative)
    ]
    assert not offenders, "paths that still say campaign:\n" + "\n".join(offenders)
