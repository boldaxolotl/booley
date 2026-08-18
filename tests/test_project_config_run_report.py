"""Tests for [developer] run_report — the optional end-of-run report.

Covers the config accessor, the developer-prompt exit rule, and the
criteria-acceptance gate. Intake seeding is covered in the harness tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from booley import project_config
from booley.harness.developer_prompt import _build_rules_section


def _project_with_toml(tmp_path: Path, body: str) -> Path:
    project = tmp_path / ".booley_project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "booley.toml").write_text(body, encoding="utf-8")
    return project


class TestIsRunReportEnabled:
    def _load_from(self, monkeypatch: pytest.MonkeyPatch, project: Path) -> bool:
        # Point discovery at the tmp project and drop the memoized config so
        # _load_config() re-reads the TOML we just wrote.
        monkeypatch.setattr(project_config, "resolve_project_dir", lambda: project)
        monkeypatch.setattr(project_config, "_CONFIG_CACHE", None)
        return project_config.is_run_report_enabled()

    def test_default_true_when_key_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        project = _project_with_toml(tmp_path, '[project]\nname = "x"\n')
        assert self._load_from(monkeypatch, project) is True

    def test_explicit_true(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        project = _project_with_toml(tmp_path, "[developer]\nrun_report = true\n")
        assert self._load_from(monkeypatch, project) is True

    def test_explicit_false(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        project = _project_with_toml(tmp_path, "[developer]\nrun_report = false\n")
        assert self._load_from(monkeypatch, project) is False


class TestPromptExitRule:
    def test_default_requires_submit_run_report(self):
        rules = _build_rules_section()
        assert "your final action is `submit_run_report`" in rules

    def test_run_report_off_keeps_conditional_justification_requirement(self):
        rules = _build_rules_section(run_report=False)
        assert "EXIT CONDITION" in rules
        assert "disables routine end-of-run reports" in rules
        assert "all mandatory and optional criteria are met" in rules
        assert "final action is `submit_run_report`" in rules
        assert "optional_criteria_justification" in rules

    def test_run_report_off_keeps_other_rules(self):
        on = _build_rules_section()
        off = _build_rules_section(run_report=False)
        for marker in ("CRITERIA FRESHNESS", "BLOCKED"):
            assert marker in on and marker in off
