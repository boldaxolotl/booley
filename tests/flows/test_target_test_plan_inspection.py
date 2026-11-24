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


def _handle(name: str, identity: str | None = None, sim: bool = True) -> SimpleNamespace:
    """A Target handle; ``sim=False`` marks one the sim Flow does not list."""
    return SimpleNamespace(name=name, selector=name, identity=identity or name, sim=sim)


def _target(
    tool: str = "verilator",
    cocotb: bool = False,
    plusargs: tuple[str, ...] = (),
    cocotb_module: object = "tb",
):
    return SimpleNamespace(
        eda_tool=tool,
        flow_options={"cocotb_module": cocotb_module} if cocotb else {},
        parameters={name: {"paramtype": "plusarg"} for name in plusargs},
    )


def _use_catalog(monkeypatch, handles, targets=None) -> None:
    """Serve *handles* from ``TargetCatalog.build``; ``targets`` maps selector to inspection."""
    targets = targets or {}

    def inspect(handle):
        return targets.get(handle.selector, _target())

    def list_handles(for_flow=None):
        return tuple(h for h in handles if h.sim or for_flow != "sim")

    catalog = SimpleNamespace(list=list_handles, inspect=inspect)
    monkeypatch.setattr(TargetCatalog, "build", classmethod(lambda _cls, _root: catalog))


def _failing_catalog(monkeypatch) -> None:
    def boom(_cls, _root):
        raise FuseSocError("no cores")

    monkeypatch.setattr(TargetCatalog, "build", classmethod(boom))


def _two_core_handles() -> tuple[SimpleNamespace, ...]:
    return tuple(_handle("sim", f"vendor:{v}:{n}:1.0#sim") for v, n in (("a", "x"), ("b", "y")))


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
    assert [t[0] for t in triples] == ["pass", "fail", "pass"]
    assert triples[0] == ("pass", _TESTS_TOML_VALID, "")
    assert "[vendor:b:y#sim] and [y#sim]" in triples[1][1]
    assert triples[1][2] == "keep one tests.toml section per Target"
    # The conflicting Target is skipped by the plan audit; only `x` gets a plan line.
    assert triples[2] == ("pass", _VERILATOR_LINE.format(name="sim"), "")


_DEAD_KEY_FIX = (
    "fix the section key (a dead key runs the testbench's default test and reports a false PASS)"
)


@pytest.mark.parametrize(
    ("handles", "text", "key", "plan_lines"),
    [
        pytest.param(
            [_handle("sim_fast", "::unit:0#sim_fast")],
            "[sim_ghost]\n",
            "sim_ghost",
            ["sim_fast"],
            id="bare",
        ),
        # `sim` is a declared Target name, but no `ghost` core declares it.
        pytest.param(
            _two_core_handles(),
            '["vendor:z:ghost#sim"]\n',
            "vendor:z:ghost#sim",
            ["sim", "sim"],
            id="qualified",
        ),
    ],
)
def test_dead_section_key_fails(tmp_path, monkeypatch, handles, text, key, plan_lines):
    _use_catalog(monkeypatch, handles)
    assert _triples(_inspect(tmp_path, text)) == [
        ("pass", _TESTS_TOML_VALID, ""),
        ("fail", f"tests.toml section [{key}] names no declared .core Target", _DEAD_KEY_FIX),
        *(("pass", _VERILATOR_LINE.format(name=name), "") for name in plan_lines),
    ]


def test_fusesoc_error_returns_silently(tmp_path, monkeypatch):
    # Enumeration failure emits nothing past the valid PASS (the structural audit reports it).
    _failing_catalog(monkeypatch)
    assert _triples(_inspect(tmp_path, "[sim_ghost]\n")) == [("pass", _TESTS_TOML_VALID, "")]


_INVALID = "tests.toml invalid: tests.toml [sim_fast]"


@pytest.mark.parametrize(
    ("text", "finding"),
    [
        pytest.param(
            '[sim_fast]\ntests = ["reset", "smoke+fast"]\n',
            f"{_INVALID}.tests: Test name 'smoke+fast' is invalid: it contains '+'",
            id="name-rule",
        ),
        pytest.param(
            '[sim_fast]\nskip = ["smoke+fast"]\n',
            f"{_INVALID}.skip: Test name 'smoke+fast' is invalid: it contains '+'",
            id="skip-name-rule",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["reset", "reset"]\n',
            f"{_INVALID}.tests names Test 'reset' more than once",
            id="duplicate-test",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["reset"]\nskip = ["reset", "reset"]\n',
            f"{_INVALID}.skip names Test 'reset' more than once",
            id="duplicate-skip",
        ),
        pytest.param("[sim_fast\n", "tests.toml invalid: ", id="malformed-toml"),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\nvariant = 1\n',
            f"{_INVALID} has unknown key 'variant'; a Target section holds only: {_SECTION_KEYS}",
            id="unknown-key",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\n[sim_fast.variants.stress]\na = []\n',
            f"{_INVALID}.variants.stress must name its default Test Variant",
            id="variant-without-default",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\nseed = "+seed=1"\n',
            f"{_INVALID}.seed template '+seed=1' must contain the plain {{seed}} field exactly once",
            id="seed-without-field",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\ndefault_seed = 0\n',
            f"{_INVALID}.default_seed 0 is out of range",
            id="default-seed-range",
        ),
        pytest.param(
            '[sim_fast]\ntests = ["stress"]\nenv = { BOOLEY_X = "1" }\n',
            f"{_INVALID}.env may not set BOOLEY_X",
            id="reserved-env",
        ),
    ],
)
def test_invalid_file_fails_with_the_validator_reason(tmp_path, text, finding):
    ((severity, message, fix),) = _triples(_inspect(tmp_path, text))
    assert severity == "fail"
    assert message.startswith(finding)
    # The hint quotes the rule sentence the validator itself reports.
    assert fix == _INVALID_FIX
    if " is invalid: " in message:
        assert message.endswith(f"Rule: {TEST_NAME_RULES}")


@pytest.mark.parametrize(
    "text",
    [
        '[sim_fast]\ntests = ["reset", "slow-first"]\nselect = "+test={name}"\n',
        '[sim_fast]\ntests = ["reset"]\nskip = ["reset"]\n',
        # With no declared Targets every key goes unjudged.
        "[sim_ghost]\n",
    ],
)
def test_valid_file_passes(tmp_path, monkeypatch, text):
    _use_catalog(monkeypatch, [])
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
            '[sim]\ntests = ["a"]\ndefault_seed = 7\n',
            _target("icarus"),
            "tests.toml cannot apply to Target 'sim': tests.toml [sim].default_seed cannot "
            "apply. Target 'sim' is unseeded: its simulator (icarus) has no seed option that "
            "Booley uses, and tests.toml declares no seed argument template. Add one (for "
            'example seed = "+seed={seed}") that the testbench reads, or drop the seed',
            id="default-seed-unseeded",
        ),
    ],
)
def test_target_checks_fail_with_the_reason(tmp_path, monkeypatch, text, target, finding):
    _use_catalog(monkeypatch, [_handle("sim")], {"sim": target})
    assert _triples(_inspect(tmp_path, text)) == [
        ("pass", _TESTS_TOML_VALID, ""),
        ("fail", finding, _PLAN_FIX),
    ]


def test_unknown_simulator_falls_back_to_its_raw_tool_name(tmp_path, monkeypatch):
    # An unrecognised tool is not a refusal: the plan is built for its raw name.
    _use_catalog(monkeypatch, [_handle("sim")], {"sim": _target("mystery-sim")})
    report = _inspect(tmp_path, '[sim]\ntests = ["a"]\n')
    assert _triples(report)[1] == (
        "pass",
        "Target 'sim' Test Runs are unseeded (Booley applies no seed to a mystery-sim Test Run)",
        "",
    )


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


def test_plan_audit_lists_only_sim_targets(tmp_path, monkeypatch):
    # The section-key audit sees every Target; the plan audit asks for sim Targets only.
    _use_catalog(monkeypatch, [_handle("sim"), _handle("synth", sim=False)])
    assert _triples(_inspect(tmp_path, '[sim]\ntests = ["a"]\n')) == [
        ("pass", _TESTS_TOML_VALID, ""),
        ("pass", _VERILATOR_LINE.format(name="sim"), ""),
    ]


def test_any_truthy_cocotb_module_makes_a_cocotb_target(tmp_path, monkeypatch):
    # Doctor's long-standing rule: a non-string truthy value still means Cocotb
    # (the Flow's own adapter rule would not), so a seed template is refused.
    target = _target("icarus", cocotb=True, cocotb_module=True)
    _use_catalog(monkeypatch, [_handle("sim")], {"sim": target})
    report = _inspect(tmp_path, '[sim]\ntests = ["a"]\nseed = "+seed={seed}"\n')
    assert report.findings[1].message.endswith("remove the seed template")
    assert "Cocotb Target" in report.findings[1].message
