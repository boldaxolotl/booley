from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from tests.architecture.contract import (
    ArchitectureContract,
    CompositionPermission,
    DirectionRule,
    LegacyWaiver,
    ModuleSelector,
    evaluate_contract,
    format_problems,
)
from tests.architecture.import_graph import Dependency


def test_selectors_distinguish_exact_prefix_and_direct_package_files() -> None:
    exact = ModuleSelector.exact("booley.flows")
    prefix = ModuleSelector.prefix("booley.flows")
    package_files = ModuleSelector.package_files("booley.flows")

    assert exact.matches("booley.flows")
    assert not exact.matches("booley.flows.policy")
    assert prefix.matches("booley.flows")
    assert prefix.matches("booley.flows.sim.flow")
    assert not prefix.matches("booley.flow")
    assert package_files.matches("booley.flows", Path("flows/__init__.py"))
    assert package_files.matches("booley.flows.policy", Path("flows/policy.py"))
    assert not package_files.matches("booley.flows.sim", Path("flows/sim/__init__.py"))
    assert not package_files.matches("booley.flows.sim.flow", Path("flows/sim/flow.py"))


def test_exact_permission_allows_only_its_named_edge() -> None:
    rule = _rule("D1", "booley.policy", "booley.presentation")
    contract = ArchitectureContract(
        rules=(rule,),
        permissions=(
            CompositionPermission(
                identifier="C1",
                rule="D1",
                source="booley.policy.entry",
                target="booley.presentation.cli",
                reason="The executable entry point composes the CLI.",
            ),
        ),
    )
    dependencies = (
        _dependency("booley.policy.entry", "booley.presentation.cli", line=3),
        _dependency("booley.policy.entry", "booley.presentation.view", line=4),
    )

    problems = evaluate_contract(dependencies, contract)

    assert len(problems) == 1
    assert problems[0].kind == "direction"
    assert problems[0].rule == "D1"
    assert problems[0].dependency == dependencies[1]
    rendered = format_problems(problems)
    assert "seed.py:4:1" in rendered
    assert "booley.policy.entry -> booley.presentation.view violates D1" in rendered
    assert "C1 allows only booley.policy.entry -> booley.presentation.cli" in rendered


def test_exact_live_waiver_suppresses_its_rule_violation() -> None:
    dependency = _dependency("booley.policy.legacy", "booley.presentation.view")
    contract = ArchitectureContract(
        rules=(_rule("D1", "booley.policy", "booley.presentation"),),
        waivers=(
            LegacyWaiver(
                identifier="W1",
                rule="D1",
                source=dependency.source,
                target=dependency.target,
                explanation="Legacy policy discovers the presentation registry.",
                retirement_issue="#123",
            ),
        ),
    )

    assert evaluate_contract((dependency,), contract) == ()


def test_missing_waiver_metadata_and_stale_waiver_fail() -> None:
    contract = ArchitectureContract(
        rules=(_rule("D1", "booley.policy", "booley.presentation"),),
        waivers=(
            LegacyWaiver(
                identifier="W1",
                rule="D1",
                source="booley.policy.legacy",
                target="booley.presentation.view",
                explanation="",
                retirement_issue="",
            ),
        ),
    )

    problems = evaluate_contract((), contract)
    rendered = format_problems(problems)

    assert [problem.kind for problem in problems] == ["metadata", "metadata", "stale-waiver"]
    assert "W1 has no design explanation" in rendered
    assert "W1 has no retirement issue" in rendered
    assert "W1 is stale" in rendered
    assert evaluate_contract((), replace(contract, waivers=())) == ()


def test_waiver_retirement_work_must_name_a_github_issue() -> None:
    contract = ArchitectureContract(
        rules=(_rule("D1", "booley.policy", "booley.presentation"),),
        waivers=(
            LegacyWaiver(
                identifier="W1",
                rule="D1",
                source="booley.policy.legacy",
                target="booley.presentation.view",
                explanation="Legacy policy discovers the presentation registry.",
                retirement_issue="eventually",
            ),
        ),
    )

    assert "W1 retirement issue is not a GitHub issue" in format_problems(
        evaluate_contract((), contract)
    )


def test_prefix_shaped_waiver_is_invalid() -> None:
    contract = ArchitectureContract(
        rules=(_rule("D1", "booley.policy", "booley.presentation"),),
        waivers=(
            LegacyWaiver(
                identifier="W1",
                rule="D1",
                source="booley.policy.*",
                target="booley.presentation.view",
                explanation="An invalid broad waiver.",
                retirement_issue="#123",
            ),
        ),
    )

    assert "W1 source is not an exact module" in format_problems(evaluate_contract((), contract))


def test_scc_subset_gate_allows_splits_and_rejects_joining_or_merging() -> None:
    approved = (
        frozenset(("booley.alpha", "booley.beta", "booley.gamma")),
        frozenset(("booley.delta", "booley.epsilon")),
    )
    contract = ArchitectureContract(approved_sccs=approved)

    split = _cycle("alpha", "beta")
    assert evaluate_contract(split, contract) == ()

    joined = (*split, *_both_ways("alpha", "zeta"))
    joined_problems = evaluate_contract(joined, contract)
    assert len(joined_problems) == 1
    assert joined_problems[0].kind == "scc"
    joined_rendered = format_problems(joined_problems)
    assert "booley.alpha, booley.beta, booley.zeta" in joined_rendered
    assert "seed.py:1:1: booley.zeta.edge -> booley.alpha.edge" in joined_rendered

    merged = (
        *_cycle("alpha", "beta"),
        *_cycle("delta", "epsilon"),
        *_both_ways("beta", "delta"),
    )
    merged_problems = evaluate_contract(merged, contract)
    assert len(merged_problems) == 1
    assert merged_problems[0].kind == "scc"
    assert "booley.alpha, booley.beta, booley.delta, booley.epsilon" in format_problems(
        merged_problems
    )


def _rule(identifier: str, source: str, target: str) -> DirectionRule:
    return DirectionRule(
        identifier=identifier,
        sources=(ModuleSelector.prefix(source),),
        targets=(ModuleSelector.prefix(target),),
        reason="Policy must not depend on presentation.",
    )


def _dependency(source: str, target: str, *, line: int = 1) -> Dependency:
    return Dependency(source, target, Path("seed.py"), line, 0)


def _cycle(*owners: str) -> tuple[Dependency, ...]:
    return tuple(
        _dependency(
            f"booley.{source}.edge",
            f"booley.{owners[(index + 1) % len(owners)]}.edge",
        )
        for index, source in enumerate(owners)
    )


def _both_ways(left: str, right: str) -> tuple[Dependency, Dependency]:
    return (
        _dependency(f"booley.{left}.edge", f"booley.{right}.edge"),
        _dependency(f"booley.{right}.edge", f"booley.{left}.edge"),
    )
