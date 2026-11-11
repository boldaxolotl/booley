"""The ``tests.toml`` and Test Run plan audit returns an ordered ``DiagnosticReport``.

Every test asserts the full ordered findings as ``(severity, message, fix)``
tuples; the strings are the ones Doctor printed before the audit moved here.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from booley.audit.diagnostic_results import DiagnosticReport
from booley.config.tests_toml_rules import TEST_NAME_RULES
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

_TESTS_TOML_VALID = (
    "tests.toml valid (section keys, Test names, Test Variants, select and seed "
    "templates well-formed)"
)
_SECTION_KEYS = "tests, test_list, skip, select, env, seed, default_seed, variants"
_INVALID_FIX = (
    f"fix the tests.toml entry named above (a Target section holds only {_SECTION_KEYS}; "
    f"{TEST_NAME_RULES})"
)
_PLAN_FIX = "fix the tests.toml entry named above"
_VERILATOR_LINE = (
    "Target '{name}' Test Runs are seeded through Verilator +verilator+seed+<seed> "
    "(Default Seed 1)"
)


def _triples(report: DiagnosticReport) -> list[tuple[str, str, str]]:
    return [(str(f.severity), f.message, f.fix) for f in report.findings]


def _inspect(root: Path, text: str | None) -> DiagnosticReport:
    """Run the owner on a Project whose ``tests.toml`` holds *text* (absent when None)."""
    booley_dir = root / ".booley"
    booley_dir.mkdir(exist_ok=True)
    if text is not None:
        (booley_dir / "tests.toml").write_bytes(text.encode("utf-8"))
    # Imported here so an absent owner fails the test body, not collection.
    from booley.flows import target_test_plan_inspection as owner

    return owner.inspect_target_test_plans(owner.TargetTestPlanInspectionRequest(root, booley_dir))


def _handle(name: str, identity: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, selector=name, identity=identity or name)


def _target(tool: str = "verilator", cocotb: bool = False, plusargs: tuple[str, ...] = ()):
    return SimpleNamespace(
        eda_tool=tool,
        flow_options={"cocotb_module": "tb"} if cocotb else {},
        parameters={name: {"paramtype": "plusarg"} for name in plusargs},
    )


def _use_catalog(monkeypatch, handles, targets=None) -> None:
    """Serve *handles* from ``TargetCatalog.build``; ``targets`` maps selector to inspection."""
    targets = targets or {}

    def inspect(handle):
        return targets.get(handle.selector, _target())

    catalog = SimpleNamespace(list=lambda **_kwargs: tuple(handles), inspect=inspect)
    monkeypatch.setattr(TargetCatalog, "build", classmethod(lambda _cls, _root: catalog))


def _failing_catalog(monkeypatch) -> None:
    def boom(_cls, _root):
        raise FuseSocError("no cores")

    monkeypatch.setattr(TargetCatalog, "build", classmethod(boom))


def _two_core_handles() -> tuple[SimpleNamespace, ...]:
    return tuple(_handle("sim", f"vendor:{v}:{n}:1.0#sim") for v, n in (("a", "x"), ("b", "y")))


def test_dead_section_key_fails(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, [_handle("sim_fast", "::unit:0#sim_fast")])
    report = _inspect(tmp_path, "[sim_ghost]\n")
    assert _triples(report) == [
        ("pass", _TESTS_TOML_VALID, ""),
        (
            "fail",
            "tests.toml section [sim_ghost] names no declared .core Target",
            "fix the section key (a dead key runs the testbench's default "
            "test and reports a false PASS)",
        ),
        ("pass", _VERILATOR_LINE.format(name="sim_fast"), ""),
    ]


def test_bare_and_qualified_keys_resolve(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, [_handle("sim_fast", "::unit:0#sim_fast")])
    report = _inspect(tmp_path, '[sim_fast]\n["::unit:0#sim_fast"]\n')
    assert _triples(report) == [
        ("pass", _TESTS_TOML_VALID, ""),
        ("pass", _VERILATOR_LINE.format(name="sim_fast"), ""),
    ]


def test_two_keys_for_one_target_fail_naming_both(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, _two_core_handles())
    report = _inspect(tmp_path, '["y#sim"]\n["vendor:b:y#sim"]\n["x#sim"]\n')
    triples = _triples(report)
    assert triples[0] == ("pass", _TESTS_TOML_VALID, "")
    fails = [t for t in triples if t[0] == "fail"]
    assert len(fails) == 1
    assert "[vendor:b:y#sim] and [y#sim]" in fails[0][1]
    assert fails[0][2] == "keep one tests.toml section per Target"


def test_qualified_key_naming_no_target_fails(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, _two_core_handles())
    # `sim` is a declared Target name, but no `ghost` core declares it.
    report = _inspect(tmp_path, '["vendor:z:ghost#sim"]\n')
    assert _triples(report)[:2] == [
        ("pass", _TESTS_TOML_VALID, ""),
        (
            "fail",
            "tests.toml section [vendor:z:ghost#sim] names no declared .core Target",
            "fix the section key (a dead key runs the testbench's default "
            "test and reports a false PASS)",
        ),
    ]


def test_fusesoc_error_returns_silently(tmp_path, monkeypatch):
    # Enumeration failure emits nothing past the valid PASS (the structural audit reports it).
    _failing_catalog(monkeypatch)
    assert _triples(_inspect(tmp_path, "[sim_ghost]\n")) == [("pass", _TESTS_TOML_VALID, "")]


def test_no_declared_targets_leave_every_key_unjudged(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, [])
    assert _triples(_inspect(tmp_path, "[sim_ghost]\n")) == [("pass", _TESTS_TOML_VALID, "")]


@pytest.mark.parametrize(
    ("text", "finding"),
    [
        pytest.param(
            '[sim_fast]\ntests = ["reset", "smoke+fast"]\n',
            "tests.toml invalid: tests.toml [sim_fast].tests: Test name "
            "'smoke+fast' is invalid: it contains '+'",
            id="plus",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["default"]\n',
            "tests.toml invalid: tests.toml [sim_fast].tests: Test name "
            "'default' is invalid: 'default' is reserved",
            id="reserved",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["-trace"]\n',
            "tests.toml invalid: tests.toml [sim_fast].tests: Test name "
            "'-trace' is invalid: it starts with '-'",
            id="leading-dash",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["reset"]\nskip = ["my test"]\n',
            "tests.toml invalid: tests.toml [sim_fast].skip: Test name "
            "'my test' is invalid: it contains whitespace",
            id="whitespace",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["reset", "reset"]\n',
            "tests.toml invalid: tests.toml [sim_fast].tests names Test 'reset' more than once",
            id="duplicate-test",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["reset"]\nskip = ["reset", "reset"]\n',
            "tests.toml invalid: tests.toml [sim_fast].skip names Test 'reset' more than once",
            id="duplicate-skip",
        ),
    ],
)
def test_name_refusal_fails_with_a_fix_hint(tmp_path, text, finding):
    (severity, message, fix), *rest = _triples(_inspect(tmp_path, text))
    assert (severity, rest) == ("fail", [])
    assert message.startswith(finding)
    # The hint quotes the rule sentence the validator itself reports.
    assert fix == _INVALID_FIX


def test_name_refusal_message_and_hint_state_the_same_rule(tmp_path):
    ((_, message, fix),) = _triples(_inspect(tmp_path, '[sim_fast]\ntests = ["a@b"]\n'))
    assert message.endswith(f"Rule: {TEST_NAME_RULES}")
    assert TEST_NAME_RULES in fix


def test_malformed_toml_fails_with_the_parser_reason(tmp_path):
    ((severity, message, fix),) = _triples(_inspect(tmp_path, "[sim_fast\n"))
    assert severity == "fail"
    assert message.startswith("tests.toml invalid: ")
    assert fix == _INVALID_FIX


@pytest.mark.parametrize("key", ["variant", "seeds", "plusargs"])
def test_unknown_section_key_fails_with_a_fix_hint(tmp_path, key):
    report = _inspect(tmp_path, f'[sim_fast]\ntests = ["stress"]\n{key} = 1\n')
    assert _triples(report) == [
        (
            "fail",
            f"tests.toml invalid: tests.toml [sim_fast] has unknown key {key!r}; "
            f"a Target section holds only: {_SECTION_KEYS}",
            _INVALID_FIX,
        )
    ]


@pytest.mark.parametrize(
    ("text", "finding"),
    [
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\n[sim_fast.variants.stress]\na = []\n',
            "tests.toml invalid: tests.toml [sim_fast].variants.stress must name "
            "its default Test Variant",
            id="variant-without-default",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\nseed = "+seed=1"\n',
            "tests.toml invalid: tests.toml [sim_fast].seed template '+seed=1' must "
            "contain the plain {seed} field exactly once",
            id="seed-without-field",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\ndefault_seed = 0\n',
            "tests.toml invalid: tests.toml [sim_fast].default_seed 0 is out of range",
            id="default-seed-range",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\nenv = { BOOLEY_X = "1" }\n',
            "tests.toml invalid: tests.toml [sim_fast].env may not set BOOLEY_X",
            id="reserved-env",
        ),
    ],
)
def test_variant_and_seed_refusals_fail(tmp_path, text, finding):
    ((severity, message, fix),) = _triples(_inspect(tmp_path, text))
    assert severity == "fail"
    assert message.startswith(finding)
    assert fix == _INVALID_FIX


def test_valid_file_passes(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, [])
    text = '[sim_fast]\ntests = ["reset", "slow-first"]\nselect = "+test={name}"\n'
    assert _triples(_inspect(tmp_path, text)) == [("pass", _TESTS_TOML_VALID, "")]


def test_pass_line_names_each_targets_seed_mechanism(tmp_path, monkeypatch):
    text = (
        '[vl]\ntests = ["a"]\ndefault_seed = 7\n'
        '[vl_tpl]\ntests = ["a"]\nseed = "+seed={seed}"\n'
        '[iv]\ntests = ["a"]\n'
        '[iv_tpl]\ntests = ["a"]\nseed = "+seed={seed}"\n'
        '[cc]\ntests = ["a"]\n'
    )
    targets = {
        "vl": _target("verilator"),
        "vl_tpl": _target("verilator"),
        "iv": _target("iverilog"),
        "iv_tpl": _target("icarus"),
        "cc": _target("icarus", cocotb=True),
    }
    _use_catalog(monkeypatch, [_handle(name) for name in targets], targets)
    assert _triples(_inspect(tmp_path, text)) == [
        ("pass", _TESTS_TOML_VALID, ""),
        (
            "pass",
            "Target 'vl' Test Runs are seeded through Verilator +verilator+seed+<seed> "
            "(Default Seed 7)",
            "",
        ),
        (
            "pass",
            "Target 'vl_tpl' Test Runs are seeded through Verilator "
            "+verilator+seed+<seed> and seed template +seed={seed} (Default Seed 1)",
            "",
        ),
        (
            "pass",
            "Target 'iv' Test Runs are unseeded (icarus without a seed template in tests.toml)",
            "",
        ),
        (
            "pass",
            "Target 'iv_tpl' Test Runs are seeded through seed template +seed={seed} "
            "(Default Seed 1)",
            "",
        ),
        (
            "pass",
            "Target 'cc' Test Runs are seeded through cocotb seed variable "
            "(COCOTB_RANDOM_SEED on 2.x, RANDOM_SEED on 1.x) (Default Seed 1)",
            "",
        ),
    ]


@pytest.mark.parametrize(
    ("text", "target", "finding"),
    [
        pytest.param(
            '[sim]\ntests = ["a"]\n[sim.variants.a]\ndefault = "v"\nv = ["+iters=1"]\n',
            _target("verilator", plusargs=("iters",)),
            "tests.toml cannot apply to Target 'sim': tests.toml [sim].variants.a.v "
            "argument '+iters=1' overrides a plusarg the Target declares; the Target "
            "owns its parameter values",
            id="overrides-plusarg",
        ),
        pytest.param(
            '[sim]\ntests = ["a"]\n[sim.variants.a]\ndefault = "v"\nv = ["+iters=1"]\n',
            _target("icarus", cocotb=True),
            "tests.toml cannot apply to Target 'sim': tests.toml [sim].variants.a.v "
            "carries arguments, but a Cocotb Target's Test Variants carry environment "
            "only; move the values into env",
            id="cocotb-arguments",
        ),
        pytest.param(
            '[sim]\ntests = ["a"]\nseed = "+seed={seed}"\n',
            _target("icarus", cocotb=True),
            "tests.toml cannot apply to Target 'sim': tests.toml [sim].seed is not "
            "allowed on a Cocotb Target: Booley seeds cocotb through its seed "
            "variable; remove the seed template",
            id="cocotb-seed-template",
        ),
    ],
)
def test_target_checks_fail_with_the_reason(tmp_path, monkeypatch, text, target, finding):
    _use_catalog(monkeypatch, [_handle("sim")], {"sim": target})
    assert _triples(_inspect(tmp_path, text)) == [
        ("pass", _TESTS_TOML_VALID, ""),
        ("fail", finding, _PLAN_FIX),
    ]


def test_default_seed_on_an_unseeded_target_fails(tmp_path, monkeypatch):
    _use_catalog(monkeypatch, [_handle("sim")], {"sim": _target("icarus")})
    report = _inspect(tmp_path, '[sim]\ntests = ["a"]\ndefault_seed = 7\n')
    assert [s for s, _, _ in _triples(report)] == ["pass", "fail"]
    assert report.findings[1].message.startswith(
        "tests.toml cannot apply to Target 'sim': tests.toml [sim].default_seed "
        "cannot apply. Target 'sim' is unseeded"
    )


def test_unknown_simulator_falls_back_to_its_raw_tool_name(tmp_path, monkeypatch):
    # An unrecognised tool is not a refusal: the plan is built for its raw name.
    _use_catalog(monkeypatch, [_handle("sim")], {"sim": _target("mystery-sim")})
    ((_, _, _), (severity, message, _)) = _triples(_inspect(tmp_path, '[sim]\ntests = ["a"]\n'))
    assert (severity, message.startswith("Target 'sim' Test Runs are ")) == ("pass", True)


def test_absent_file_skips_without_naming_a_fallback_file(tmp_path):
    assert _triples(_inspect(tmp_path, None)) == [
        ("skip", "tests.toml not present (each sim Target runs its testbench's default test)", "")
    ]


def test_plan_audit_catalog_failure_keeps_only_the_valid_pass(tmp_path, monkeypatch):
    # The section-key audit builds the catalog once; the plan audit builds it again
    # and meets the failure there (a Target-less Project passes the first).
    builds = iter([SimpleNamespace(list=lambda **_kwargs: ())])

    def build(_cls, _root):
        try:
            return next(builds)
        except StopIteration:
            raise FuseSocError("no cores") from None

    monkeypatch.setattr(TargetCatalog, "build", classmethod(build))
    assert _triples(_inspect(tmp_path, '[sim]\ntests = ["a"]\n')) == [
        ("pass", _TESTS_TOML_VALID, "")
    ]


def test_uninspectable_target_is_left_to_the_resolve_check(tmp_path, monkeypatch):
    handles = [_handle("broken"), _handle("fine")]

    def inspect(handle):
        if handle.selector == "broken":
            raise FuseSocError("cannot resolve")
        return _target()

    catalog = SimpleNamespace(list=lambda **_kwargs: tuple(handles), inspect=inspect)
    monkeypatch.setattr(TargetCatalog, "build", classmethod(lambda _cls, _root: catalog))
    assert _triples(_inspect(tmp_path, '[broken]\ntests = ["a"]\n[fine]\ntests = ["a"]\n')) == [
        ("pass", _TESTS_TOML_VALID, ""),
        ("pass", _VERILATOR_LINE.format(name="fine"), ""),
    ]
