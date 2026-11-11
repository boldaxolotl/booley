"""The cocotb Target diagnostic owner: rules asserted on its returned report."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import pytest

from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.config.project_config import normalize_tests_toml
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import FuseSocError

_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {{files: [rtl/dut.sv]}}
  tb: {{files: [{tb_file}], tags: [{tags}]}}
targets:
  sim:
    flow: sim
    flow_options: {{tool: verilator, cocotb_module: test_dut}}
    filesets: [rtl, tb]
    toplevel: dut
"""

_PLAIN_TB = "import cocotb\n\n@cocotb.test()\nasync def test_reset(dut):\n    pass\n"

_FACTORY_TB = """\
import cocotb
from cocotb.regression import TestFactory


async def run_test(dut, data_width=8):
    pass


factory = TestFactory(run_test)
factory.add_option("data_width", [8, 16])
factory.generate_tests()
"""

_PARAMETRIZED_TB = """\
import cocotb


@cocotb.test()
@cocotb.parametrize(("ifg", [12, 0]))
async def run_test_tx(dut, ifg=12):
    pass
"""


def _write_project(
    root: Path,
    *,
    tb_body: str | None = _PLAIN_TB,
    tb_file: str = "tb/test_dut.py",
    tags: str = "tb",
) -> None:
    """A project whose only sim Target is a Cocotb Target (ADR 0034).

    ``tb_body=None`` leaves the module file off disk.
    """
    (root / "design.core").write_text(
        _CORE.format(tb_file=tb_file, tags=tags), encoding="utf-8", newline="\n"
    )
    (root / "rtl").mkdir(exist_ok=True)
    (root / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
    if tb_body is not None:
        (root / "tb").mkdir(exist_ok=True)
        (root / "tb" / "test_dut.py").write_text(tb_body, encoding="utf-8", newline="\n")


def _request(root: Path, sections: dict[str, Any] | None = None) -> Any:
    """Build the owner's request from the Project's real Targets."""
    from booley.flows.sim.backends import cocotb_target_checks

    return cocotb_target_checks.CocotbTargetRequest(
        tuple(TargetCatalog.build(root).list()),
        normalize_tests_toml(sections or {}),
    )


def _inspect(root: Path, tests: list[str] | None = None, **section: Any) -> DiagnosticReport:
    """Run the owner against *root* with a tests.toml ``[sim]`` section."""
    from booley.flows.sim.backends import cocotb_target_checks

    if tests is not None:
        section["tests"] = tests
    sections = {"sim": section} if section else {}
    return cocotb_target_checks.inspect_cocotb_targets(_request(root, sections))


def _rows(report: DiagnosticReport) -> list[tuple[Severity, str, str, str | None, str | None]]:
    return [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in report.findings]


def _has_cocotb(root: Path) -> bool:
    from booley.flows.sim.backends import cocotb_target_checks

    return cocotb_target_checks.project_has_cocotb_target(root)


class TestCocotbTestNames:
    """B6: generated cocotb names cannot be grepped for as `def <name>(`."""

    @pytest.mark.parametrize(
        ("body", "base"),
        [
            (_FACTORY_TB, "run_test"),
            (_PARAMETRIZED_TB, "run_test_tx"),
            (
                _FACTORY_TB.replace("import TestFactory", "import TestFactory as Factory").replace(
                    "TestFactory(run_test)", "Factory(run_test)"
                ),
                "run_test",
            ),
            (
                _PARAMETRIZED_TB.replace("import cocotb", "import cocotb as c").replace(
                    "@cocotb.", "@c."
                ),
                "run_test_tx",
            ),
        ],
    )
    def test_generating_base_name_is_deferred(self, tmp_path, body, base):
        _write_project(tmp_path, tb_body=body)
        report = _inspect(tmp_path, [base])
        skips = [f.message for f in report.findings if f.severity is Severity.SKIP]
        assert any(base in m and "generating function" in m and "rendered IDs" in m for m in skips)
        assert not any("tests.toml names all present" in f.message for f in report.findings)

    @pytest.mark.parametrize(
        "names",
        [
            ["test_plain"],
            ["test_plain", "run_test_tx"],
            ["test_plain", "run_test_tx/ifg=12", "typo"],
        ],
    )
    def test_generating_module_defers_every_registered_name(self, tmp_path, names):
        body = _PARAMETRIZED_TB + "\n@cocotb.test()\nasync def test_plain(dut): pass\n"
        _write_project(tmp_path, tb_body=body)
        report = _inspect(tmp_path, names)
        skips = [
            f.message
            for f in report.findings
            if f.severity is Severity.SKIP and "cannot be verified statically" in f.message
        ]
        assert len(skips) == 1
        assert f"{len(names)} tests.toml name(s)" in skips[0]
        assert all(name in skips[0] for name in names)
        assert not any("tests.toml names all present" in f.message for f in report.findings)

    @pytest.mark.parametrize("decorator", ["@cocotb.test()", "@cocotb.test", "@test()"])
    def test_generation_words_in_comments_do_not_suppress_plain_checks(self, tmp_path, decorator):
        body = (
            "import cocotb\nfrom cocotb import test\n"
            f"# TestFactory generate_tests cocotb.parametrize\n{decorator}\n"
            "async def test_reset(dut): pass\n"
        )
        _write_project(tmp_path, tb_body=body)
        report = _inspect(tmp_path, ["test_reset"])
        assert any(
            f.severity is Severity.PASS and "tests.toml names all present" in f.message
            for f in report.findings
        )

    @pytest.mark.parametrize(
        "body",
        [
            "async def test_reset(dut): pass\n",
            "# def test_reset(dut):\n",
            "class TB:\n    def test_reset(self): pass\n",
        ],
    )
    def test_plain_function_search_does_not_earn_a_pass(self, tmp_path, body):
        _write_project(tmp_path, tb_body=body)
        report = _inspect(tmp_path, ["test_reset"])
        assert any(
            f.severity is Severity.WARN
            and "not found as" in f.message
            and "test_reset" in f.message
            for f in report.findings
        )
        assert not any("tests.toml names all present" in f.message for f in report.findings)

    @pytest.mark.parametrize(
        "body",
        [
            "not python!",
            "\x00",
            "import cocotb\n@cocotb.test(name='other')\nasync def test_reset(dut): pass\n",
        ],
    )
    def test_unverifiable_source_never_passes_names(self, tmp_path, body):
        _write_project(tmp_path, tb_body=body)
        report = _inspect(tmp_path, ["test_reset"])
        assert any(
            f.severity is Severity.SKIP and "cannot be verified statically" in f.message
            for f in report.findings
        )
        assert not any("tests.toml names all present" in f.message for f in report.findings)

    @pytest.mark.parametrize(
        ("body", "names", "word"),
        [
            (_FACTORY_TB, ["run_test_001", "run_test_002"], "factory"),
            (_PARAMETRIZED_TB, ["run_test_tx/ifg=12", "run_test_tx/ifg=0"], "parameterized"),
        ],
        ids=["factory", "parametrized"],
    )
    def test_generated_names_are_reported_unverifiable_not_missing(
        self, tmp_path, body, names, word
    ):
        _write_project(tmp_path, tb_body=body)
        report = _inspect(tmp_path, names)
        assert not any("not found as functions" in f.message for f in report.findings)
        assert any(
            f.severity is Severity.SKIP
            and "cannot be verified statically" in f.message
            and word in f.message
            for f in report.findings
        )
        assert not report.active_findings

    def test_hand_written_names_still_warn_when_absent(self, tmp_path: Path):
        # The softening is gated on the factory call: a module WITHOUT one that
        # names a test it does not define is still a real, greppable mismatch.
        _write_project(tmp_path)
        report = _inspect(tmp_path, ["test_reset", "test_typo"])
        warns = [f for f in report.findings if f.severity is Severity.WARN]
        assert len(warns) == 1
        assert "not found as functions" in warns[0].message
        assert "test_typo" in warns[0].message
        assert "test_reset" not in warns[0].message
        assert (warns[0].check_id, warns[0].subject) == ("cocotb.test-name-missing", "sim")

    def test_all_names_present_passes(self, tmp_path: Path):
        _write_project(tmp_path)
        report = _inspect(tmp_path, ["test_reset"])
        assert [f.message for f in report.findings if f.severity is Severity.PASS] == [
            "cocotb Target 'sim' module file present (test_dut.py)",
            "cocotb Target 'sim' tests.toml names all present in test_dut.py",
        ]
        assert not report.active_findings

    def test_no_declared_names_skips_the_name_scan(self, tmp_path: Path):
        _write_project(tmp_path)
        report = _inspect(tmp_path)
        assert [f.message for f in report.findings] == [
            "cocotb Target 'sim' module file present (test_dut.py)"
        ]


def test_select_template_fails_against_the_qualified_section(tmp_path: Path):
    _write_project(tmp_path)
    request = _request(tmp_path, {"demo#sim": {"select": "+t={name}"}})
    from booley.flows.sim.backends import cocotb_target_checks

    report = cocotb_target_checks.inspect_cocotb_targets(request)
    fails = [f for f in report.findings if f.severity is Severity.FAIL]
    assert [(f.message, f.fix) for f in fails] == [
        (
            "cocotb Target 'sim' declares a tests.toml `select` plusarg template",
            "remove `select` — cocotb test selection is the COCOTB_TEST_FILTER env var "
            "built from the `tests` list (ADR 0034)",
        )
    ]


def test_duplicate_section_keys_leave_the_target_to_the_section_key_audit(tmp_path: Path):
    _write_project(tmp_path)
    sections = {
        "demo#sim": {"select": "+t={name}"},
        "::demo:0#sim": {"tests": ["test_typo"]},
    }
    from booley.flows.sim.backends import cocotb_target_checks

    report = cocotb_target_checks.inspect_cocotb_targets(_request(tmp_path, sections))
    assert report.findings == ()


def test_fileset_rules_report_check_ids_and_subjects(tmp_path: Path):
    """Each advisory warning is waivable by its check ID and Target subject."""
    # No fileset file names the module.
    _write_project(tmp_path, tb_file="tb/other.py")
    missing = _inspect(tmp_path)
    assert _rows(missing) == [
        (
            Severity.WARN,
            "cocotb Target 'sim': no fileset file matches its cocotb_module "
            "'test_dut' (test_dut.py)",
            "add the Python testbench to a tagged fileset with copyto: test_dut.py",
            "cocotb.module-file-missing",
            "sim",
        )
    ]
    # The module is in an untagged fileset.
    _write_project(tmp_path, tags="rtl")
    untagged = _inspect(tmp_path)
    assert [(f.check_id, f.subject) for f in untagged.findings if f.severity is Severity.WARN] == [
        ("cocotb.tb-tag-missing", "sim")
    ]
    assert untagged.findings[-1].message == "cocotb Target 'sim' module file present (test_dut.py)"
    # The fileset names the module but the file is not on disk.
    (tmp_path / "tb" / "test_dut.py").unlink()
    absent = _inspect(tmp_path)
    assert [(f.check_id, f.subject) for f in absent.findings] == [
        ("cocotb.tb-tag-missing", "sim"),
        ("cocotb.module-file-missing", "sim"),
    ]
    assert absent.findings[-1].message == (
        f"cocotb Target 'sim': module file {tmp_path / 'tb' / 'test_dut.py'} not found on disk"
    )
    assert absent.findings[-1].fix == "author the Python testbench (or fix the fileset path)"


def test_copyto_destination_names_the_module(tmp_path: Path):
    _write_project(tmp_path, tb_body=None, tb_file="{tb/source.py: {copyto: sub/test_dut.py}}")
    (tmp_path / "tb").mkdir()
    (tmp_path / "tb" / "source.py").write_text(_PLAIN_TB, encoding="utf-8")
    report = _inspect(tmp_path)
    assert [f.severity for f in report.findings] == [Severity.PASS]


def test_targets_without_a_cocotb_module_have_no_findings(tmp_path: Path):
    (tmp_path / "design.core").write_text(
        "CAPI=2:\nname: ::demo:0\ntargets:\n  sim: {flow: sim, flow_options: {tool: verilator}}\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _inspect(tmp_path, ["anything"]) == DiagnosticReport()


def test_unreadable_core_has_no_module_file(tmp_path: Path):
    _write_project(tmp_path)
    request = _request(tmp_path)
    (tmp_path / "design.core").write_text("not: [valid", encoding="utf-8")
    from booley.flows.sim.backends import cocotb_target_checks

    report = cocotb_target_checks.inspect_cocotb_targets(request)
    assert [f.check_id for f in report.findings] == ["cocotb.module-file-missing"]


def test_cocotb_targets_report_in_name_order(tmp_path: Path):
    (tmp_path / "design.core").write_text(
        "CAPI=2:\nname: ::demo:0\ntargets:\n"
        "  zed: {flow: sim, flow_options: {tool: verilator, cocotb_module: m}}\n"
        "  alpha: {flow: sim, flow_options: {tool: verilator, cocotb_module: m}}\n",
        encoding="utf-8",
        newline="\n",
    )
    report = _inspect(tmp_path)
    assert [f.subject for f in report.findings] == ["alpha", "zed"]


def test_request_carries_only_targets_and_sections():
    from booley.flows.sim.backends import cocotb_target_checks

    request_type = cocotb_target_checks.CocotbTargetRequest
    fields = dataclasses.fields(request_type)
    assert {field.name for field in fields} == {"targets", "tests_sections"}
    assert not any("ProjectAudit" in str(field.type) for field in fields)


def test_project_has_cocotb_target(tmp_path: Path):
    assert _has_cocotb(tmp_path) is False  # no .core at all
    (tmp_path / "plain.core").write_text(
        "CAPI=2:\nname: ::plain:0\ntargets:\n  sim: {flow: sim, flow_options: {tool: verilator}}\n",
        encoding="utf-8",
        newline="\n",
    )
    assert _has_cocotb(tmp_path) is False
    _write_project(tmp_path)
    assert _has_cocotb(tmp_path) is True


def test_project_has_cocotb_target_is_false_when_the_catalog_cannot_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A Project whose Targets cannot be listed asks no cocotb check of anyone."""
    _write_project(tmp_path)

    def refuse(_root: Path) -> TargetCatalog:
        raise FuseSocError("catalog cannot be built")

    monkeypatch.setattr(TargetCatalog, "build", refuse)
    assert _has_cocotb(tmp_path) is False
