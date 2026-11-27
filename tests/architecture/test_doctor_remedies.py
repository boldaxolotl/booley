"""Architecture guard: every Doctor FAIL and WARN names a remedy.

A remedy is: the fix field, or failing that the message,
holds a backticked command, a single-quoted literal (an apostrophe in a word does not count),
or a clause that starts with an action verb. The verbs live in one place,
``doctor_remedies.REMEDY_VERBS``. The scan reads ``src/booley`` statically, so the reviewed
lists below stand for what it cannot judge:

* ``_BASELINE``: sites that violate the rule today, one entry per site. Fixing a site without
  removing its entry fails the test, and so does a new site without a remedy.
* ``_NO_REMEDY``: sites for which no action can exist, each with a one-line reason. None so far:
  a FAIL or WARN nobody can act on belongs in NOTE.
* ``_UNRESOLVED``: sites whose message or fix is built at runtime (exception text, a field of an
  owner record, a joined list). The scan judges literal text only, so each is listed with its
  reason and a new one fails until someone reads it. Changing the runtime value of a listed site
  is not caught.
* ``_NON_DOCTOR_PRINTERS``: modules that print a literal FAIL or WARN status line outside Doctor.

What the scan cannot reach, besides the runtime text above:

* Findings forwarded from owner records the scan does not enumerate (``BootstrapFinding``,
  the model probe, the upgrade presentation, ``SkippedEndpoint``,
  ``BooleyDirPinError``): the forwarding site is an ``_UNRESOLVED`` entry. Forwards of
  ``EnvironmentFinding``, ``EdaEnvironmentFinding``, ``ConfigFinding`` and ``DiagnosticFinding``
  are checked where their owner builds them.
* A sink stored in a container or attribute, or handed to code outside ``src/booley``:
  ``test_every_sink_value_is_traced`` reports each such place.
* A helper called through a receiver other than ``self``, a module alias, or a plain name.
* A severity held in a variable (``Finding(severity, ...)``), and NOTE, PASS, SKIP, and waived
  lines: only literal FAIL and WARN are in scope.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from tests.architecture import doctor_remedies as scan
from tests.source_checkout_support import source_checkout_files

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGE = _ROOT / "src" / "booley"

# Modules the audit names: each must contribute at least one site, so a scan that silently
# stopped seeing one fails here.
_AUDITED_MODULES = (
    "harness/doctor.py",
    "harness/host_diagnostics.py",
    "harness/setup/readiness.py",
    "harness/auto_doctor.py",
    "audit/host_environment.py",
    "audit/eda_environment.py",
    "audit/project_schema.py",
    "audit/flow_schema.py",
    "audit/agent_schema.py",
    "audit/core_tree_inspection.py",
    "flows/target_test_plan_inspection.py",
    "flows/runtime_reality.py",
    "flows/fpga/vivado_startup.py",
    "flows/sim/backends/cocotb_target_checks.py",
    "mcp/server_inspection.py",
    "runtime/inspection.py",
    "runtime/core_resolution.py",
    "runtime/core_repository_hygiene.py",
    "runtime/incontainer_setup.py",
    "runtime/project_image.py",
    "runtime/vaporview.py",
)

_NON_DOCTOR_PRINTERS = {
    "harness/setup/common.py": "`booley init` print helpers, not a Doctor report",
    "harness/bootstrap_cli.py": "`booley bootstrap` output, not a Doctor report",
}

_NO_REMEDY: dict[str, str] = {}


_BASELINE: tuple[str, ...] = (
    'fail - flows/fpga/vivado_startup.py::inspect_vivado_startup "fpga: Vivado batch startup: runtime/prob"',
    "fail - flows/runtime_reality.py::_check_session_binary \"f'{label} (runtime/probe error: {exc})'\"",
    'fail - harness/doctor.py::_doctor_flow_command "f\'{label} could not enter the Sandbox: {"',
    'fail - harness/doctor.py::_prepare_selftest_invocation "f\'{label} could not enter the Sandbox: {"',
    'warn handles.map-size harness/doctor.py::_check_handle_map_size "job handle map"',
    'fail - audit/agent_schema.py::audit_agent_table "booley.toml [agent] uses retired key(s):"',
    'fail - audit/host_environment.py::_run_container_probe "container runtime permission denied"',
    'fail - audit/project_schema.py::_feedback_field_findings "booley.toml [feedback].redact_identifier"',
    'fail - audit/project_schema.py::audit_specialist_table "booley.toml [specialists."',
    'fail - audit/project_schema.py::audit_specialist_table "booley.toml [specialists."',
    'fail - audit/project_schema.py::audit_specialist_table "booley.toml [specialists] must be a tabl"',
    'fail - audit/project_schema.py::audit_stealth_table "booley.toml [stealth].ignore_native_core"',
    'fail - audit/project_schema.py::audit_worktree_table "booley.toml"',
    'fail - flows/fpga/vivado_startup.py::inspect_vivado_startup "fpga: Vivado batch startup: runtime did "',
    'fail - flows/fpga/vivado_startup.py::inspect_vivado_startup "fpga: Vivado batch startup:"',
    'fail - harness/doctor.py::_check_git_conflicts "git operation in progress:"',
    'fail - harness/doctor.py::_check_sim_target_tb_staging "sim Target \'"',
    'fail - harness/doctor.py::_check_sim_target_tb_staging "sim Target \'"',
    'fail - harness/doctor.py::_enumerate_core_audit_targets "project has no .core: a resolvable FuseS"',
    "fail - harness/doctor.py::_execute_selftest \"f'{label} failed to start: {exc}'\"",
    "fail - harness/doctor.py::_execute_selftest \"f'{label} timed out after {timeout_s}s'\"",
    'fail - harness/doctor.py::_grade_flow_check_result "f\'{label} failed with exit {result.retur"',
    'fail - harness/doctor.py::_grade_flow_check_result "f\'{label} produced incomplete synthesis "',
    'fail - harness/doctor.py::_grade_selftest_result "f\'{label} FALSE-PASSED (exit 0) on a kno"',
    "fail - harness/doctor.py::_run_container_checks \"f'{image} image missing'\"",
    'fail - harness/doctor.py::_run_developer_probe "Specialist provider probe could not comp"',
    "fail - harness/doctor.py::_run_flow_check_subprocess \"f'{label} failed to start: {exc}'\"",
    "fail - harness/doctor.py::_run_flow_check_subprocess \"f'{label} timed out after {timeout_s}s'\"",
    'fail - harness/host_diagnostics.py::_inspect_host_installation "current Booley process does not match th"',
    'fail - harness/host_diagnostics.py::_inspect_package "booley package not importable"',
    "fail - harness/setup/readiness.py::_load_toml \"f'{path.name} missing'\"",
    "fail - harness/setup/readiness.py::_load_toml \"f'{path.name} unreadable: {exc}'\"",
    'fail - harness/setup/readiness.py::_report_repository_format "f\'{repository}: extensions.relativeWorkt"',
    'fail - harness/setup/readiness.py::_report_repository_line_endings "Project Root ("',
    'fail - harness/setup/readiness.py::_report_repository_line_endings "Project Root ("',
    'fail - harness/setup/readiness.py::_validate_booley_toml "Booley Directory missing:"',
    'fail - harness/setup/readiness.py::load_project "Booley Directory not found"',
    'fail - mcp/server_inspection.py::_grade_payload "MCP interactive log setup failed"',
    'fail - runtime/core_resolution.py::_execute ".core resolvability could not complete i"',
    'fail - runtime/core_resolution.py::_grade ".core Target \'"',
    'fail - runtime/core_resolution.py::_grade ".core resolvability produced incomplete "',
    'fail - runtime/inspection.py::_check_job_logs_tracked "job_logs/ has"',
    'fail - runtime/inspection.py::_check_issued_license_relay "License Profile authority is invalid:"',
    'fail - runtime/inspection.py::_credential_policy_inspectable "Sandbox credential mount expectation is "',
    'fail - runtime/inspection.py::_probe_mounted_vivado "Vivado runtime identity probe failed:"',
    'fail - runtime/inspection.py::_probe_mounted_vivado "mounted Vivado did not report exact vers"',
    'fail agent.credential-missing harness/doctor.py::_check_cli_credentials "message"',
    'fail agent.credential-missing harness/doctor.py::_check_cli_credentials "message"',
    'warn agent.backend-health harness/doctor.py::_check_agent_backend_health "worker backend ("',
    'warn agent.backend-health harness/doctor.py::_check_agent_backend_health "worker backend health check failed:"',
    'warn agent.credential-missing harness/doctor.py::_check_cli_credentials "message"',
    'warn agent.credential-missing harness/doctor.py::_check_cli_credentials "message"',
    'warn agent.credential-policy harness/doctor.py::_warn_ignored_gateway_handoff "BOOLEY_PRIMARY_AUTH=gateway is honored o"',
    'warn agent.model-fallback harness/doctor.py::_probe_model_list "Specialist model list for [models]"',
    'warn agent.rotation-risk harness/doctor.py::_check_oauth_token "f\'{label}: stored credential is readable"',
    'warn agent.rotation-risk harness/doctor.py::_report_refreshing_login "message"',
    'warn agent.subscription-credential-expired harness/doctor.py::_check_subscription_creds_health "subscription login at"',
    'warn cocotb.module-file-missing flows/sim/backends/cocotb_target_checks.py::_inspect_target "cocotb Target \'"',
    'warn cocotb.tb-tag-missing flows/sim/backends/cocotb_target_checks.py::_inspect_target "cocotb Target \'"',
    'warn cocotb.test-name-missing flows/sim/backends/cocotb_target_checks.py::_check_test_names "cocotb Target \'"',
    'warn config.flow-knob-ignored audit/flow_schema.py::_ignored_flow_knob_findings "booley.toml [flows."',
    'warn config.flow-section-missing audit/flow_schema.py::_required_flow_findings "booley.toml [flows."',
    'warn config.project-metadata audit/project_schema.py::audit_project_table "booley.toml [project].name missing or em"',
    'warn config.project-metadata audit/project_schema.py::audit_project_table "booley.toml missing [project] table"',
    'warn config.unknown-table audit/project_schema.py::audit_known_tables "booley.toml ["',
    "warn core.interface-toplevel harness/doctor.py::_check_toplevel_interface_ports \"f\"{ref.flow} Target '{name}' toplevel '{\"",
    "warn flow.selftest-evidence-unvalidated harness/doctor.py::_grade_sim_bad_selftest \"f'{label} fail-path unvalidated - {exc}'\"",
    'warn flow.selftest-overlay-build-failed harness/doctor.py::_grade_sim_bad_selftest "f\'{label}: bad overlay no longer builds "',
    "warn git.autocrlf-risk harness/setup/readiness.py::_report_autocrlf_risk \"f'{identity}: {message}'\"",
    'warn git.line-endings-unreadable harness/setup/readiness.py::_report_unreadable_line_endings "line endings:"',
    'warn git.line-endings-unreadable harness/setup/readiness.py::inspect_line_endings "line endings: could not inspect Project "',
    'warn guidance.session-runtime-scope harness/setup/readiness.py::_check_guidance_runtime_note "could not read"',
    'warn host.bootstrap-warning harness/host_diagnostics.py::_add_bootstrap "Host Bootstrap"',
    "warn interactive.devcontainer-drift runtime/inspection.py::_spec_warning \"message + '; ' + request.remedy + ''.joi\"",
    'warn interactive.mcp-tool-advisory-missing mcp/server_inspection.py::_grade_payload "MCP missing documented interactive endpo"',
    'warn project-data.destination-branch-unreadable harness/doctor.py::_warn_project_data_branch_unreadable "could not inspect"',
    'warn project.git-excludes-missing runtime/inspection.py::_check_devcontainer_excludes "git info/exclude missing Booley entry:"',
    'warn project.gitignore harness/doctor.py::_check_project_gitignore ".booley/.gitignore is missing"',
    'warn project.gitignore harness/doctor.py::_check_project_gitignore ".booley/.gitignore missing; transient Bo"',
    'warn project.gitignore harness/doctor.py::_check_project_gitignore "could not read"',
    'warn sandbox.image-stale harness/doctor.py::_check_custom_image_freshness "custom sandbox image \'"',
    'warn sandbox.uid-probe harness/doctor.py::_check_container_uid "could not probe container uid"',
    'warn sandbox.uid-probe harness/doctor.py::_check_container_uid "could not probe container uid:"',
    'warn sandbox.uid-probe harness/doctor.py::_check_container_uid "could not stat"',
    'warn sandbox.uid-probe harness/doctor.py::_check_container_uid "unexpected container uid output:"',
    'warn sandbox.unproxied harness/doctor.py::_check_current_runtime_egress "Unproxied Sandbox (BOOLEY_EGRESS=unproxi"',
    'warn sim.pass-sentinel-missing harness/doctor.py::_warn_missing_sim_sentinel "sim Target \'"',
    'warn tickets.leftover-guidance harness/doctor.py::_check_leftover_guidance "could not read leftover Ticket guidance "',
)

_SCHEMA_FINDINGS = "message and fix come from the schema owner's finding tuples"
_RUNTIME_TUPLE = "message and fix come from an unpacked runtime tuple"
_EXCEPTION_TEXT = "message is an exception's text, so a remedy in it is invisible to the scan"
_RETIRED_HINT = "fix is the retired-table hint from a constant mapping, looked up at runtime"
_OWNER_RECORD = "forwards an owner record's message and fix, built where the owner constructs it"
_MODEL_PROBE = "forwards a model probe's message and fix, built where the probe runs"
_BOOTSTRAP_FIX = "fix is BootstrapFinding.doctor_fix, built at runtime by the bootstrap owner"
_DISCOVERY_ERROR = "fix is the first reported discovery error, built at runtime"
_RUNTIME_FIX = "the fix is computed at runtime, so the scan cannot read it"
_PIN_ERROR = "message is a BooleyDirPinError detail and its remedy comes from the error"
_PROBE_WARNING = "forwards a probe result's warning text, built by the probe"
_UPGRADE_PRESENTATION = (
    "forwards the upgrade presentation's summary and action, built by its owner"
)
_SKIPPED_ENDPOINT = "fix is SkippedEndpoint.fix, built at runtime by endpoint validation"

_UNRESOLVED: dict[str, str] = {
    'fail - audit/core_tree_inspection.py::inspect_core_security ".core security ["': "fix is CoreViolation.fix, built per violation by core_security",
    'warn core.security-scope-advisory audit/core_tree_inspection.py::inspect_core_security ".core security ["': "fix is CoreViolation.fix, built per violation by core_security",
    'fail - runtime/vaporview.py::_report_per_root "message"': "message joins the per-root findings built at runtime",
    'fail - audit/core_tree_inspection.py::_check_schema ".core schema"': _SCHEMA_FINDINGS,
    'fail - audit/project_schema.py::_feedback_field_findings "message"': _EXCEPTION_TEXT,
    'fail - audit/project_schema.py::audit_features_table "message"': _EXCEPTION_TEXT,
    'fail - audit/project_schema.py::audit_interactive_table "message"': _EXCEPTION_TEXT,
    'fail - audit/project_schema.py::audit_known_tables "booley.toml [mcp_tools] is retired"': _RETIRED_HINT,
    'fail - audit/project_schema.py::audit_sandbox_table "message"': _EXCEPTION_TEXT,
    'fail - audit/project_schema.py::audit_specialist_table "message"': _EXCEPTION_TEXT,
    'fail - flows/target_test_plan_inspection.py::_audit_section_keys "str(exc)"': _EXCEPTION_TEXT,
    'fail - harness/doctor.py::_Reporter.diagnostics "finding.message"': _OWNER_RECORD,
    'fail - harness/doctor.py::_check_doctor_target_compatibility "*failure"': _RUNTIME_TUPLE,
    'fail - harness/doctor.py::_check_doctor_targets "*failure"': _RUNTIME_TUPLE,
    'warn sandbox.swap-unbacked harness/doctor.py::_check_sandbox_swap "*problem"': _RUNTIME_TUPLE,
    'fail - harness/doctor.py::_check_riscv_toolchain "finding.message"': _OWNER_RECORD,
    'fail - harness/doctor.py::_probe_model_list "probe.message"': _MODEL_PROBE,
    'fail - harness/doctor.py::_probe_one_model "probe.message"': _MODEL_PROBE,
    'fail - harness/doctor.py::run_doctor "str(exc)"': _EXCEPTION_TEXT,
    'fail - harness/host_diagnostics.py::_add_bootstrap "Host Bootstrap"': _BOOTSTRAP_FIX,
    'fail - harness/host_diagnostics.py::_add_environment "finding.message"': _OWNER_RECORD,
    'fail - harness/host_diagnostics.py::_inspect_host_installation "str(exc)"': _EXCEPTION_TEXT,
    'fail - harness/setup/readiness.py::_add_config_audit "finding.message"': _OWNER_RECORD,
    'fail - mcp/server_inspection.py::_grade_payload "MCP tool discovery reported"': _DISCOVERY_ERROR,
    'fail - runtime/inspection.py::_check_issued_license_relay "issued License Profile lacks an immutabl"': _RUNTIME_FIX,
    'fail - runtime/inspection.py::_check_issued_session_runtime "Sandbox host issuance is invalid:"': _RUNTIME_FIX,
    'fail - runtime/inspection.py::_check_runtime_booley_version "host and issued Sandbox Image contain di"': _RUNTIME_FIX,
    'fail - runtime/inspection.py::_cli_set_current "devcontainer.json Agent CLI record is ma"': _RUNTIME_FIX,
    'fail - runtime/inspection.py::_cli_set_current "devcontainer.json Agent CLIs"': _RUNTIME_FIX,
    'fail - runtime/inspection.py::_inspect_active_sandbox "str(exc)"': _EXCEPTION_TEXT,
    'fail - runtime/inspection.py::_inspect_live_runtime "live Sandbox state differs from current "': _RUNTIME_FIX,
    'fail agent.credential-missing harness/doctor.py::_check_gateway_auth "str(exc)"': _EXCEPTION_TEXT,
    'fail agent.credential-policy harness/doctor.py::_check_cli_credentials "str(exc)"': _EXCEPTION_TEXT,
    'fail agent.credential-policy harness/doctor.py::_check_gateway_model_pin "str(exc)"': _EXCEPTION_TEXT,
    'fail agent.credential-policy harness/doctor.py::_report_agent_app_choice "str(exc)"': _EXCEPTION_TEXT,
    'fail project.booley-dir-pin harness/doctor.py::_booley_dir_pin_failure "exc.detail"': _PIN_ERROR,
    'warn - harness/doctor.py::_Reporter.diagnostics "finding.message"': _OWNER_RECORD,
    'warn - harness/doctor.py::_check_current_runtime_git_identity "result.warning"': _PROBE_WARNING,
    'warn - harness/doctor.py::_check_upgrade_review "presentation.summary"': _UPGRADE_PRESENTATION,
    'warn - harness/host_diagnostics.py::_add_environment "finding.message"': _OWNER_RECORD,
    'warn - harness/setup/readiness.py::_add_config_audit "finding.message"': _OWNER_RECORD,
    'warn project.custom-endpoint-skipped harness/doctor.py::_check_custom_endpoints "custom endpoint"': _SKIPPED_ENDPOINT,
}


@functools.cache
def _source_files() -> tuple[Path, ...]:
    listed = source_checkout_files(_ROOT, "src/booley")
    return tuple(path for path in listed if path.suffix == ".py")


@functools.cache
def _real_sites() -> tuple[scan.EmissionSite, ...]:
    return tuple(scan.scan_emission_sites(_PACKAGE, _source_files()))


def _scan_text(tmp_path: Path, source: str) -> list[scan.EmissionSite]:
    """Scan one module, ``booley/m.py``, written under *tmp_path*."""
    package = tmp_path / "booley"
    package.mkdir()
    (package / "m.py").write_bytes(source.encode())
    return scan.scan_emission_sites(package, [package / "m.py"])


def _problems(sites: list[scan.EmissionSite], **lists: object) -> list[str]:
    defaults: dict[str, object] = {"baseline": (), "no_remedy": {}, "unresolved": {}}
    return scan.guard_problems(sites, **{**defaults, **lists})  # type: ignore[arg-type]


def test_every_doctor_fail_and_warn_names_a_remedy() -> None:
    sites = _real_sites()
    problems = scan.guard_problems(
        sites,
        baseline=_BASELINE,
        no_remedy=_NO_REMEDY,
        unresolved=_UNRESOLVED,
        non_doctor_printers=_NON_DOCTOR_PRINTERS,
    )
    assert problems == []
    seen = {site.path for site in sites}
    assert [module for module in _AUDITED_MODULES if module not in seen] == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("run `booley init`", True),
        ("delete [x] from booley.toml", True),
        ("x is stale; re-run Doctor", True),
        ("'docker' group missing", True),
        ("booley bootstrap", False),
        ("check permissions on the file", False),
        ("Booley's cache isn't writable", False),
        ("see '\x00' and `\x00`", False),
    ],
)
def test_remedy_rule(text: str, expected: bool) -> None:
    assert scan.names_a_remedy(text) is expected


_REPORTER_SOURCE = """
from booley.audit.doctor_waivers import warning


def checks(reporter, value):
    reporter.fail_("disk broke")
    reporter.warn_(warning("disk.full", "disk is full"))
    reporter.fail_("disk broke", "run `df -h`")
    reporter.fail_("free disk space with `df -h`")
    reporter.fail_("disk gone", f"'{value}'")
    payload(make(reporter.fail_))


def make(fail):
    def check(description, fix=""):
        fail(description, fix)

    return check


def payload(check):
    check("tool missing")
"""


def test_new_reporter_finding_without_remedy_fails_the_guard(tmp_path: Path) -> None:
    sites = _scan_text(tmp_path, _REPORTER_SOURCE)
    new = [p for p in _problems(sites, baseline=_BASELINE) if p.startswith("new FAIL/WARN")]
    heads = sorted(problem.split(" (")[0].split("::")[1] for problem in new)
    assert heads == [
        'checks "disk broke"',
        'checks "disk gone"',
        'checks "disk is full"',
        'payload "tool missing"',
    ]
    assert any("m.py:" in problem for problem in new)


_TWO_WARNINGS_SOURCE = """
def check(reporter):
    reporter.warn_(warning("x.id", "first thing"), "run `fix-it`")
    reporter.warn_(warning("x.id", "second thing"))
"""


def test_fixed_baseline_entry_must_leave_the_baseline(tmp_path: Path) -> None:
    sites = _scan_text(tmp_path, _TWO_WARNINGS_SOURCE)
    first = 'warn x.id m.py::check "first thing"'
    second = 'warn x.id m.py::check "second thing"'
    assert [s.key for s in sites] == [first, second]
    problems = _problems(sites, baseline=(first,))
    assert problems == [
        f"new FAIL/WARN without a remedy: {second} (m.py:4)",
        f"now names a remedy; remove it from the baseline: {first}",
    ]
    assert _problems(sites, baseline=(second,)) == []


def test_exception_reasons_are_one_line(tmp_path: Path) -> None:
    sites = _scan_text(
        tmp_path, 'def f(reporter):\n    reporter.fail_("a")\n    reporter.fail_("b")\n'
    )
    key_a, key_b = (site.key for site in sites)
    problems = _problems(sites, no_remedy={key_a: "", key_b: "first line\nsecond line"})
    assert problems == [
        f"no-remedy entry needs a one-line reason: {key_a}",
        f"no-remedy entry needs a one-line reason: {key_b}",
    ]
    assert _problems(sites, no_remedy={key_a: "no action exists", key_b: "same"}) == []


def test_unresolved_site_must_be_listed(tmp_path: Path) -> None:
    sites = _scan_text(
        tmp_path, "def f(reporter, exc):\n    reporter.fail_(str(exc), exc.remedy)\n"
    )
    (key,) = (site.key for site in sites)
    assert [site.verdict for site in sites] == ["unresolved"]
    assert _problems(sites) == [f"unresolved site not listed: {key}"]
    assert _problems(sites, unresolved={key: "text is built at runtime"}) == []
    stale = _problems(sites, unresolved={key: "text is built at runtime", "x": "gone"})
    assert stale == ["stale unresolved entry: x"]


def test_non_doctor_printer_entry_must_name_a_printer(tmp_path: Path) -> None:
    source = "def f(ts):\n    print(ts.status_line(ts.LineStatus.FAIL, 'boom'))\n"
    sites = _scan_text(tmp_path, source)
    assert [site.form for site in sites] == ["status_line"]
    printers = {"m.py": "prints init output"}
    assert _problems(sites, non_doctor_printers=printers) == []
    assert _problems(sites, non_doctor_printers={"gone.py": "x"}) == [
        "stale non-Doctor printer entry: gone.py",
        'new FAIL/WARN without a remedy: fail - m.py::f "boom" (m.py:2)',
    ]


def _untraced_in(tmp_path: Path, source: str) -> list[str]:
    """Run the sink-value scan over *source* written as a one-module package."""
    package = tmp_path / "booley"
    package.mkdir()
    (package / "m.py").write_bytes(source.encode())
    return scan.untraced_sink_values(package, [package / "m.py"])


def test_every_sink_value_is_traced(tmp_path: Path) -> None:
    assert scan.untraced_sink_values(_PACKAGE, _source_files()) == []
    source = (
        "def f(reporter, holder):\n"
        '    registry = {"fail": reporter.fail_}\n'
        "    holder.sink = reporter.warn_\n"
        "    return registry\n"
    )
    assert _untraced_in(tmp_path, source) == ["m.py:2", "m.py:3"]


_KEYWORD_SOURCE = """
from booley.audit.diagnostic_results import DiagnosticFinding, Severity


def checks():
    return DiagnosticFinding(severity=Severity.FAIL, message="disk broke", fix="")
"""


def test_keyword_severity_is_scanned(tmp_path: Path) -> None:
    sites = _scan_text(tmp_path, _KEYWORD_SOURCE)
    assert [(site.key, site.verdict) for site in sites] == [
        ('fail - m.py::checks "disk broke"', "violation")
    ]


_REBOUND_SOURCE = """
def check(warn):
    warn = _warning_sink(warn, "x.id")
    warn("disk is full")
"""


def test_rebound_sink_parameter_counts_each_call_once(tmp_path: Path) -> None:
    source = _REBOUND_SOURCE + "\n\ndef run(reporter):\n    check(reporter.warn_)\n"
    sites = _scan_text(tmp_path, source)
    assert [site.key for site in sites] == ['warn x.id m.py::check "disk is full"']


def test_keyword_callback_to_unknown_callee_is_untraced(tmp_path: Path) -> None:
    source = (
        "def f(reporter):\n"
        "    register(on_fail=reporter.fail_)\n"
        "    known(fail=reporter.warn_)\n"
        "\n\ndef known(fail):\n    fail('x')\n"
    )
    assert _untraced_in(tmp_path, source) == ["m.py:2"]
