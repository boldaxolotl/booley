"""Architecture guard: Simulation Record names say "record", not "campaign".

The Simulation Record package and every other source, test and QA module name the concept
with Record names. A name that still says campaign names another concept and is listed
below with its reason; it is allowed only in the file, and for scoped entries the function or
class, that is listed.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_RECORD_PACKAGE = _ROOT / "src" / "booley" / "flows" / "sim" / "record"
_FORBIDDEN = "campaign"


class _DefinedNames(ast.NodeVisitor):
    """Collect (line, scope, name) for every identifier a module defines.

    *scope* is the dotted name of the enclosing classes and functions (the function or
    class itself for its own name and parameters), or <module> at the top level.
    """

    def __init__(self) -> None:
        self.found: list[tuple[int, str, str]] = []
        self._stack: list[str] = []

    def _add(self, line: int, name: str) -> None:
        self.found.append((line, ".".join(self._stack) or "<module>", name))

    def _add_arguments(self, arguments: ast.arguments) -> None:
        every_argument = [*arguments.posonlyargs, *arguments.args, *arguments.kwonlyargs]
        if arguments.vararg is not None:
            every_argument.append(arguments.vararg)
        if arguments.kwarg is not None:
            every_argument.append(arguments.kwarg)
        for argument in every_argument:
            self._add(argument.lineno, argument.arg)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._stack.append(node.name)
        self._add(node.lineno, node.name)
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._stack.append(node.name)
        self._add(node.lineno, node.name)
        self._add_arguments(node.args)
        self.generic_visit(node)
        self._stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._stack.append(node.name)
        self._add(node.lineno, node.name)
        self._add_arguments(node.args)
        self.generic_visit(node)
        self._stack.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._add_arguments(node.args)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store):
            self._add(node.lineno, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Store):
            self._add(node.lineno, node.attr)
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self._add(node.lineno, node.name)
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                self._add(node.lineno, alias.asname)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.asname:
                self._add(node.lineno, alias.asname)

    def _add_exports(self, targets: list[ast.expr], value: ast.expr | None) -> None:
        if any(
            isinstance(target, ast.Name) and target.id == "__all__" for target in targets
        ) and isinstance(value, (ast.List, ast.Tuple)):
            for element in value.elts:
                if isinstance(element, ast.Constant) and isinstance(element.value, str):
                    self._add(element.lineno, element.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        self._add_exports(node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._add_exports([node.target], node.value)
        self.generic_visit(node)


def _defined_names(tree: ast.AST) -> list[tuple[int, str, str]]:
    """Return (line, scope, name) for every identifier a module defines."""
    collector = _DefinedNames()
    collector.visit(tree)
    return collector.found


# Coverage Record names kept on purpose: shared QA helpers (#2911 owns QA mission assets) and the
# persisted waiver keys (#2909 keeps the waiver candidate identity and path fields and the
# Coverage reference kind in waivers).
_COVERAGE_RECORD_NAMES: dict[str, frozenset[str]] = {
    "qa/shared/coverage/evaluator/measurements.py": frozenset(
        {
            "CAMPAIGN_SCHEMA",
            "read_campaign",
        }
    ),
    "qa/shared/coverage/faults/approvals.py": frozenset(
        {
            "bind:campaign",
        }
    ),
    "qa/shared/coverage/faults/campaign.py": frozenset(
        {
            "nested_campaign",
            "rebind_reference:campaign",
        }
    ),
    "src/booley/flows/sim/waiver_candidates.py": frozenset(
        {
            "campaign_id",
            "campaign_path",
        }
    ),
    "src/booley/goals/waiver_binding.py": frozenset(
        {
            "campaign_id",
            "campaign_path",
            "campaign_reference",
        }
    ),
}

# Mutation Tester and Target campaign names, which keep their name.
_MUTATION_TARGET_NAMES: dict[str, frozenset[str]] = {
    "src/booley/agent_workspace/isolation.py": frozenset(
        {
            "_campaign_roots",
        }
    ),
    "src/booley/criteria/templates.py": frozenset(
        {
            "TARGET_CAMPAIGN_CRITERIA",
            "_TARGET_CAMPAIGN_PARAM_REGISTRY",
        }
    ),
    "src/booley/flows/mutation/flow.py": frozenset(
        {
            "_apply_campaign_defaults",
            "_attach_campaign_artifacts",
            "_attach_campaign_directories",
            "_campaign_for_target",
            "_campaign_manifest",
            "_copy_campaign_dir",
            "_copy_campaign_file",
            "_resolve_campaign",
            "_run_campaign",
            "_stage_campaign_files",
            "_target_campaign",
            "_write_campaign_manifest",
            "campaign",
            "campaign_dir",
            "campaign_error",
        }
    ),
    "src/booley/flows/mutation/memory_claims.py": frozenset({"CampaignClaims"}),
    "src/booley/flows/target_campaign.py": frozenset(
        {
            "CampaignResults",
            "CampaignUnit",
            "CampaignUnitResult",
            "TargetCampaign",
            "all_campaign_results_match",
            "describe_target_campaign",
            "resolve_target_campaign",
        }
    ),
    "src/booley/flows/target_criteria.py": frozenset(
        {
            "CampaignScopeError",
            "resolve_campaign_scope",
        }
    ),
    "tests/criteria/test_campaign_templates.py": frozenset(
        {
            "campaign",
            "test_coverage_campaign_expands_shared_policy_per_target",
            "test_coverage_campaign_rejects_duplicate_target_ownership",
            "test_coverage_campaign_rejects_invalid_authoring",
            "test_mutation_campaign_expands_target_and_parameters",
            "test_mutation_campaign_rejects_incoherent_counts",
        }
    ),
    "tests/flows/mutation/test_flow.py": frozenset(
        {
            "_assert_durable_campaign",
            "_campaign",
            "_target_campaign",
            "campaign",
            "campaign_dir",
            "test_campaign_artifacts_show_each_tests_baseline_duration_and_time_limit",
            "test_campaign_without_build_failures_names_no_replacement",
            "test_campaign_runs_the_baseline_then_each_mutant",
            "test_happy_path_publishes_campaign_artifacts",
        }
    ),
    "tests/flows/mutation/test_memory_claims.py": frozenset(
        {
            "_campaign",
            "test_an_item_that_ran_alone_over_budget_warns_in_the_campaign_result",
            "test_campaign_processes_rank_as_the_held_claim",
            "test_queued_claim_is_withdrawn_when_the_campaign_is_cancelled",
            "test_queued_simulation_claim_is_withdrawn_when_the_campaign_is_cancelled",
        }
    ),
    "tests/flows/test_target_campaign.py": frozenset(
        {
            "campaign",
            "test_all_skipped_campaign_is_rejected",
            "test_batched_campaign_launches_one_process_per_given_batch",
            "test_campaign_aggregation_cannot_pass_vacuously",
            "test_campaign_domain_does_not_import_presentation_layers",
        }
    ),
    "tests/flows/test_transport_contract.py": frozenset(
        {
            "run_campaign",
        }
    ),
}

# Goal-side names left to #3045 (the freshness category and unsettled campaigns).
_GOAL_NAMES: dict[str, frozenset[str]] = {
    "src/booley/flows/source_fingerprint.py": frozenset(
        {
            "_campaign_core_files",
            "_campaign_files",
            "campaign",
            "campaign_fingerprint",
        }
    ),
    "src/booley/goals/simulation_record_accounting.py": frozenset(
        {
            "unsettled_campaigns",
        }
    ),
    "tests/flows/test_source_fingerprint_campaign.py": frozenset(
        {
            "campaign",
            "test_campaign_fingerprint_names_files_through_symlinked_booley_dir",
            "test_campaign_fingerprint_tracks_tests_and_selected_core",
            "test_campaign_fingerprint_tracks_transitive_core_changes",
            "test_campaign_fingerprint_uses_resolved_booley_dir",
        }
    ),
    "tests/goals/test_freshness.py": frozenset(
        {
            "test_target_less_tb_and_consumed_campaign_are_filtered",
        }
    ),
}


def _kept_entries() -> dict[str, frozenset[str]]:
    """Merge the keep lists by file. An entry is name or Scope.qualname:name."""
    merged: dict[str, set[str]] = {}
    for kept in (_COVERAGE_RECORD_NAMES, _MUTATION_TARGET_NAMES, _GOAL_NAMES):
        for relative, entries in kept.items():
            merged.setdefault(relative, set()).update(entries)
    return {relative: frozenset(entries) for relative, entries in merged.items()}


def test_simulation_record_package_defines_only_record_names() -> None:
    sources = sorted(_RECORD_PACKAGE.glob("*.py"))
    assert sources, f"no modules found under {_RECORD_PACKAGE}"
    offenders = [
        f"{source.name}:{line} {name}"
        for source in sources
        for line, _scope, name in _defined_names(ast.parse(source.read_bytes()))
        if _FORBIDDEN in name.lower()
    ]
    assert not offenders, "Simulation Record package defines campaign names:\n" + "\n".join(
        offenders
    )


def test_simulation_record_names_outside_the_package_use_record_names() -> None:
    kept = _kept_entries()
    used: set[tuple[str, str]] = set()
    offenders: list[str] = []
    for path in source_checkout_files(_ROOT, "src", "tests", "qa"):
        if path.suffix != ".py":
            continue
        relative = path.relative_to(_ROOT).as_posix()
        allowed = kept.get(relative, frozenset())
        for line, scope, name in _defined_names(ast.parse(path.read_bytes())):
            if _FORBIDDEN not in name.lower():
                continue
            entry = next((e for e in (name, f"{scope}:{name}") if e in allowed), None)
            if entry is None:
                offenders.append(f"{relative}:{line} {scope}:{name}")
            else:
                used.add((relative, entry))
    assert not offenders, (
        "names that say campaign for the Simulation Record; rename them to Record names, or "
        "list a name of another concept with its reason:\n" + "\n".join(offenders)
    )
    stale = sorted(
        f"{relative} {entry}"
        for relative, entries in kept.items()
        for entry in entries
        if (relative, entry) not in used
    )
    assert not stale, "keep entries that no longer match a defined name:\n" + "\n".join(stale)
