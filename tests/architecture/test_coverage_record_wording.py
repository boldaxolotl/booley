"""Architecture guard: the Coverage Record is said as "Coverage Record", never as a campaign.

Part 6 of the #2910 split. Messages, help text, docstrings, comments and names that mean the
Coverage Record say "Coverage Record". The word "campaign" stays only where it names something
else or where a persisted spelling or an old name is quoted; every such place is listed below
with its reason. The lowercase phrase "coverage record" names no other concept.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.architecture.test_simulation_record_identifiers import _defined_names
from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_SELF = "tests/architecture/test_coverage_record_wording.py"
_WORD = "campaign"
# The lowercase phrase is spelled by concatenation so that this module does not match itself.
_LOWERCASE_PHRASE = "coverage" + " record"

# Whole files or trees that keep the word, by path prefix, with the reason.
_KEPT_TREES: dict[str, str] = {
    # Mutation Tester (out of scope).
    "src/booley/flows/mutation/": "Mutation Tester campaign",
    "tests/flows/mutation/": "Mutation Tester campaign tests",
    # Target campaign (kept by the brief).
    "src/booley/flows/target_campaign.py": "Target campaign",
    "src/booley/flows/target_criteria.py": "Target campaign scope",
    "tests/flows/test_target_campaign.py": "Target campaign tests",
    "tests/criteria/test_campaign_templates.py": "Target campaign authoring tests (#3912 kept it)",
    # Goal-side names left to #3045.
    "src/booley/flows/source_fingerprint.py": "the Goal 'campaign' freshness category",
    "tests/flows/test_source_fingerprint_campaign.py": "the Goal 'campaign' freshness category",
    "tests/golden/": "Goal-side and persisted spellings in captured goldens",
    # Old on-disk records and the #2909 refusal.
    "src/booley/flows/sim/record/codec.py": "retired schema labels (#2909)",
    "src/booley/flows/sim/record_layout.py": "refusal of the older campaign directories (#2909)",
    "tests/flows/fixtures/booley_0_3_0/": "captured Booley 0.3.0 records",
    "tests/flows/fixtures/booley_0_4_0/": "captured Booley 0.4.0 records",
    "tests/runtime/fixtures/directory_migration/": "captured older records",
    "tests/flows/sim/test_coverage_record_names.py": "proves the old names are refused",
    "tests/flows/sim/test_simulation_record_names.py": "proves the old names are refused",
    "tests/flows/sim/test_simulation_record_documents.py": "proves the old names are refused",
    "tests/flows/sim/test_simulation_output_record_names.py": "proves the old names are refused",
    "tests/flows/sim/test_simulation_record_layout.py": "proves the old directories are refused",
    "tests/flows/sim/test_simulation_record_unsupported_formats.py": "proves old records are refused",
    "tests/flows/sim/test_simulation_record_acceptance.py": "persisted acceptance spellings",
    # QA asset names and the older records their validators read.
    "tests/qa/": "QA asset names and the older records their validators read",
    # Guards and tools that name the spelling they forbid or rewrite.
    "tests/architecture/test_simulation_record_identifiers.py": "guard of campaign names",
    "tests/architecture/test_simulation_record_paths.py": "guard of campaign paths",
    "tests/architecture/test_simulation_record_prose.py": "guard of Simulation Campaign prose",
    _SELF: "this guard",
    "tests/ci/test_terminology_diff_exclusions.py": "tests the campaign-to-record rename rule",
}

# Spellings allowed in any file: persisted keys, kinds and schema ids that #2909 kept.
_PERSISTED: tuple[str, ...] = (
    "campaign_id",
    "campaign_path",
    "campaign_reference",
    "coverage_campaign_reference",
    "nested_campaign_sha256",
    "simulation_campaign",
    "adopted_campaign",
    "unsettled_campaigns",
    "goal-campaign",
    "booley.coverage-campaign",
    "booley.simulation-campaign",
    "booley.cocotb-campaign",
)

# Exact substrings, per file, that the file may keep, with the reason.
_KEPT_SPELLINGS: dict[str, tuple[str, ...]] = {
    # Mutation Tester campaign: its snapshot roots, workspace directory and Flow prose.
    "src/booley/agent_workspace/isolation.py": ("_campaign_roots",),
    "src/booley/flows/endpoint_admission.py": ("mutation campaign's",),
    "tests/agent_workspace/test_snapshot_sources.py": ("arbitrary-old-campaign",),
    "tests/dev_support/test_flow_specialist_reference.py": (
        "| Campaign |",
        "owns the campaign,",
    ),
    "tests/dev_support/test_mutation_variants.py": ('"campaign/variants"',),
    "tests/dev_support/test_mutation_workspace.py": ('"campaign"',),
    "tests/flows/test_transport_contract.py": ("run_campaign", "_run_campaign"),
    "tests/mcp_tools/test_specialist_workspace.py": ("mutation/campaign/variants",),
    # Target campaign: the paired-campaign Criteria and the vacuous-suite refusal.
    "src/booley/criteria/templates.py": (
        "_TARGET_CAMPAIGN_PARAM_REGISTRY",
        "TARGET_CAMPAIGN_CRITERIA",
        "Target campaign",
        "Paired campaign Target",
        "campaign target must",
    ),
    "src/booley/flows/target_test_suite.py": ("vacuous campaign",),
    "tests/architecture/booley_contract.py": ("target_campaign",),
    "tests/architecture/test_booley_contract.py": ("target_campaign",),
    # Goal-side names left to #3045: the 'campaign' freshness category and its fingerprint.
    "src/booley/criteria/categories.py": ('"campaign"',),
    "src/booley/criteria/state.py": ("campaign resets",),
    # Old 0.3.0/0.4.0 names Booley refuses or reads: placeholder, report fields, directories.
    "src/booley/flows/sim/config.py": ('field == "campaign"', "{campaign}"),
    "src/booley/presentation/report_results.py": ('"campaigns"', "detail.campaigns"),
    "tests/harness/test_doctor_selftest_builtin.py": ('["campaigns"]',),
    "tests/runtime/test_execution_records.py": ("campaign-child-executions",),
    "tests/runtime/test_execution_recovery.py": ("campaign-child-executions",),
    "tests/runtime/test_project_runtime_gitignore.py": ("campaign-child-executions",),
    "tests/flows/sim/test_coverage_flow.py": ('"campaign" not in prompt',),
    "tests/flows/sim/test_coverage_transaction.py": ('"coverage_campaign" not in',),
    "tests/flows/sim/test_endpoint_record_lifecycle.py": ("campaign_error",),
    # Scratch-prefix cleanup of what Booley 0.4.0 left behind.
    "src/booley/runtime/owned_scratch.py": ("booley-coverage-campaign-",),
    # The written coverage_record_id value starts with "campaign:" (a persisted value, #2909).
    "src/booley/flows/sim/coverage_transaction.py": ('"campaign:{',),
    "tests/flows/sim/test_coverage_canonical_forms.py": ("campaign:sim_counter",),
    "tests/flows/sim/test_coverage_policy.py": ("campaign:sim_counter",),
    "tests/flows/sim/test_coverage_provisional.py": ("campaign:sim_counter", "campaign:other"),
    "tests/flows/sim/test_coverage_record.py": ("campaign:sim_counter",),
    "tests/flows/sim/test_coverage_waivers.py": ("campaign:sim_counter",),
    "tests/mcp_tools/test_coverage_evidence.py": ('"campaign:"',),
}


def _checked_files() -> list[Path]:
    """Source and test text files; skips binary payloads and the packaged Markdown under src."""
    return [
        path
        for path in source_checkout_files(_ROOT, "src", "tests")
        if not (path.suffix == ".md" and path.relative_to(_ROOT).parts[0] == "src")
    ]


def _text(path: Path) -> str | None:
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None


def _masked(line: str, spellings: tuple[str, ...]) -> str:
    for spelling in sorted((*_PERSISTED, *spellings), key=len, reverse=True):
        left = r"(?<![A-Za-z0-9_])" if re.match(r"\w", spelling) else ""
        right = r"(?![A-Za-z0-9_])" if re.search(r"\w$", spelling) else ""
        line = re.sub(f"{left}{re.escape(spelling)}{right}", "", line)
    return line


def test_campaign_is_said_only_for_kept_names() -> None:
    offenders: list[str] = []
    used_trees: set[str] = set()
    used_spellings: set[tuple[str, str]] = set()
    for path in _checked_files():
        relative = path.relative_to(_ROOT).as_posix()
        tree = next((prefix for prefix in _KEPT_TREES if relative.startswith(prefix)), None)
        text = _text(path)
        if text is None:
            continue
        if tree is not None:
            if _WORD in text.lower():
                used_trees.add(tree)
            continue
        spellings = _KEPT_SPELLINGS.get(relative, ())
        for spelling in spellings:
            if spelling in text:
                used_spellings.add((relative, spelling))
        for number, line in enumerate(text.splitlines(), 1):
            # Masking is costly; a line without the word needs none (a 60s timeout bounds this scan).
            if _WORD in line.lower() and _WORD in _masked(line, spellings).lower():
                offenders.append(f"{relative}:{number}: {line.strip()[:100]}")
    assert not offenders, (
        "campaign is said for the Coverage Record; say Coverage Record, or list the kept "
        "spelling with its reason:\n" + "\n".join(offenders)
    )
    stale_trees = sorted(set(_KEPT_TREES) - used_trees)
    assert not stale_trees, "kept trees with no campaign left:\n" + "\n".join(stale_trees)
    stale = sorted(
        f"{relative} {spelling!r}"
        for relative, spellings in _KEPT_SPELLINGS.items()
        for spelling in spellings
        if (relative, spelling) not in used_spellings
    )
    assert not stale, "kept spellings that no file line uses:\n" + "\n".join(stale)


def _names_containing(path: str, fragment: str) -> list[str]:
    tree = ast.parse((_ROOT / path).read_bytes())
    return sorted(
        f"{path}:{line} {scope}:{name}"
        for line, scope, name in _defined_names(tree)
        if fragment in re.sub("_", "", name).lower()
    )


def test_coverage_record_names_one_concept() -> None:
    found: list[str] = []
    for path in _checked_files():
        relative = path.relative_to(_ROOT).as_posix()
        text = _text(path)
        if text is None or relative == _SELF:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _LOWERCASE_PHRASE in line:
                found.append(f"{relative}:{number}: {line.strip()[:100]}")
    assert not found, (
        "the lowercase phrase names the Criteria authoring item or a native counter entry; "
        "say 'coverage authoring record' or 'native counter entry', or 'Coverage Record' for "
        "the Coverage Record:\n" + "\n".join(found)
    )
    assert not _names_containing("src/booley/criteria/templates.py", "coveragerecord")
    assert not _names_containing("src/booley/flows/sim/verilator_coverage.py", "nativerecord")
