"""Tests for harness.criteria_acceptance — criteria-based acceptance logic."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.dev_support.criteria import cycle_count_criterion_key
from booley.dev_support.development_state import (
    SOURCE_FINGERPRINT_DETAIL_KEY,
    DevelopmentState,
    compute_source_fingerprint,
)
from booley.ticket_board.criteria_acceptance import (
    CriteriaVerdict,
    build_criteria_summary_lines,
    check_criteria_acceptance,
    format_criteria_verdict,
    refresh_verification_freshness,
)

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")

# ---------------------------------------------------------------------------
# CriteriaVerdict
# ---------------------------------------------------------------------------


class TestCriteriaVerdict:
    def test_passed_true_when_review(self):
        v = CriteriaVerdict(disposition="review")
        assert v.passed is True

    def test_passed_false_when_failed(self):
        v = CriteriaVerdict(disposition="failed")
        assert v.passed is False

    def test_passed_false_when_blocked(self):
        v = CriteriaVerdict(disposition="blocked")
        assert v.passed is False

    def test_default_values(self):
        v = CriteriaVerdict(disposition="review")
        assert v.total == 0
        assert v.met == 0
        assert v.unmet_mandatory == []
        assert v.blocked_reason == ""


# ---------------------------------------------------------------------------
# check_criteria_acceptance — mock DevelopmentState
# ---------------------------------------------------------------------------


@dataclass
class _FakeCriterion:
    met: bool
    mandatory: bool
    detail: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    stale: bool = False
    ever_met: bool = False
    ever_failed: bool = False


@dataclass
class _FakeState:
    slug: str = "test-ticket"
    criteria: dict = field(default_factory=dict)

    def summary(self):
        return {
            "total": len(self.criteria),
            "met": sum(1 for c in self.criteria.values() if c.met),
            "mandatory": sum(1 for c in self.criteria.values() if c.mandatory),
            "mandatory_met": sum(1 for c in self.criteria.values() if c.mandatory and c.met),
        }


class TestCheckCriteriaAcceptance:
    def _write_state_and_check(self, tmp_path: Path, fake_state: _FakeState) -> CriteriaVerdict:
        state_path = tmp_path / "booley_state.json"
        # Write a dummy file so exists() check passes
        state_path.write_text("{}", encoding="utf-8")

        with patch("booley.dev_support.development_state.DevelopmentState") as mock_cls:
            mock_cls.load.return_value = fake_state
            return check_criteria_acceptance(state_path)

    def test_missing_state_file(self, tmp_path: Path):
        verdict = check_criteria_acceptance(tmp_path / "nonexistent.json")
        assert verdict.disposition == "failed"
        assert "not found" in verdict.blocked_reason

    def test_no_criteria(self, tmp_path: Path):
        verdict = self._write_state_and_check(tmp_path, _FakeState(criteria={}))
        assert verdict.disposition == "failed"
        assert "no criteria" in verdict.blocked_reason

    def test_all_mandatory_met(self, tmp_path: Path):
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "lint_clean": _FakeCriterion(met=True, mandatory=True),
                "nice_to_have": _FakeCriterion(met=False, mandatory=False),
                # _report_submitted is the internal hidden gate; developer
                # must have called submit_run_report before transitioning to
                # review (see check_criteria_acceptance).
                "_report_submitted": _FakeCriterion(
                    met=True,
                    mandatory=True,
                    detail={"unmet_optional_criteria": ["nice_to_have"]},
                ),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "review"
        assert verdict.mandatory == 2
        assert verdict.mandatory_met == 2
        assert verdict.total == 3
        assert verdict.met == 2

    def test_unmet_mandatory(self, tmp_path: Path):
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "lint_clean": _FakeCriterion(met=False, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "failed"
        assert "lint_clean" in verdict.unmet_mandatory

    def test_blocked_reason_blocks_when_mandatory_unmet(self, tmp_path: Path):
        """_blocked_reason blocks when some mandatory criteria are unmet."""
        state = _FakeState(
            criteria={
                "_blocked_reason": _FakeCriterion(
                    met=True,
                    mandatory=False,
                    detail={"reason": "Agent needs human input"},
                ),
                "sim_pass": _FakeCriterion(met=False, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "blocked"
        assert "human input" in verdict.blocked_reason

    def test_stale_blocked_reason_ignored_when_all_mandatory_met(self, tmp_path: Path):
        """Stale _blocked_reason from prior run must not override a successful re-run."""
        state = _FakeState(
            criteria={
                "_blocked_reason": _FakeCriterion(
                    met=True,
                    mandatory=False,
                    detail={"reason": "Stale reason from run-1"},
                ),
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "lint_clean": _FakeCriterion(met=True, mandatory=True),
                "_report_submitted": _FakeCriterion(met=True, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "review"
        assert verdict.blocked_reason == ""

    def test_blocked_reason_unmet_ignored(self, tmp_path: Path):
        """_blocked_reason with met=False should NOT trigger blocked disposition."""
        state = _FakeState(
            criteria={
                "_blocked_reason": _FakeCriterion(met=False, mandatory=False, detail={}),
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "_report_submitted": _FakeCriterion(met=True, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "review"

    def test_strict_model_contract_accepts_model_evidence(self, tmp_path: Path):
        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.init_criteria(
            {"sim_pass_sim_model": True, "_report_submitted": True},
            criterion_params={
                "sim_pass_sim_model": {
                    "target": "sim_model",
                    "subject": "model",
                    "required_tests": ["model_reset"],
                    "minimum_total": 1,
                }
            },
            strict=True,
        )
        state.set_criterion(
            "sim_pass_sim_model",
            True,
            detail={
                "verification_subject": "model",
                "selected_tests": ["model_reset", "model_mask"],
                "passed_tests": ["model_reset", "model_mask"],
                "tests_passed": 2,
            },
        )
        state.set_criterion("_report_submitted", True)
        state.save()

        verdict = check_criteria_acceptance(state_path)

        assert verdict.disposition == "review"

    def test_strict_fail_to_pass_accepts_recorded_red_then_green(self, tmp_path: Path):
        state_path = tmp_path / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.init_criteria(
            {"sim_pass_sim_uart": True, "_report_submitted": True},
            criterion_params={
                "sim_pass_sim_uart": {
                    "target": "sim_uart",
                    "from_state": "fail",
                    "test_selector": "test_transmit",
                }
            },
            strict=True,
        )
        state.set_criterion(
            "sim_pass_sim_uart",
            False,
            detail={
                "failed_tests": ["test_transmit"],
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl", "tb"],
                    "fingerprint": "red-source-fingerprint",
                    "target": "sim_uart",
                },
            },
        )
        state.set_criterion(
            "sim_pass_sim_uart",
            True,
            detail={
                "selected_tests": ["test_transmit"],
                "passed_tests": ["test_transmit"],
                "tests_passed": 1,
            },
        )
        state.set_criterion("_report_submitted", True)
        state.save()

        verdict = check_criteria_acceptance(state_path)

        assert verdict.disposition == "review"
        assert verdict.unverified_transitions == []

    def test_review_blocked_when_report_not_submitted(self, tmp_path: Path):
        """All visible mandatory met but report not submitted -> failed."""
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "lint_clean": _FakeCriterion(met=True, mandatory=True),
                "_report_submitted": _FakeCriterion(met=False, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["_report_submitted"]

    def test_review_blocked_when_report_criterion_absent(self, tmp_path: Path):
        """All visible mandatory met but _report_submitted criterion missing -> failed.

        Defends against a partially-initialized state where the report
        criterion never got injected.
        """
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["_report_submitted"]

    def test_report_gate_skipped_when_run_report_disabled(self, tmp_path: Path, monkeypatch):
        """[developer] run_report = false -> absent criterion must not fail.

        With the report disabled, intake never seeds `_report_submitted`, so
        the acceptance gate must go straight to review on green criteria.
        """
        from booley.config import project_config

        # NOTE: do NOT monkeypatch the module attribute (project_config,
        # "RUN_REPORT") — saving the "old value" triggers the PEP 562 lazy
        # __getattr__, and teardown would then materialize a permanent module
        # global that shadows every later config load in the test session.
        # Patching the memoized cache dict restores cleanly instead.
        monkeypatch.setattr(project_config, "_CONFIG_CACHE", {"RUN_REPORT": False})
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.disposition == "review"

    def test_disabled_report_still_required_for_unmet_optional(self, tmp_path: Path, monkeypatch):
        from booley.config import project_config

        monkeypatch.setattr(project_config, "_CONFIG_CACHE", {"RUN_REPORT": False})
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "mutation_score": _FakeCriterion(met=False, mandatory=False),
            }
        )

        verdict = self._write_state_and_check(tmp_path, state)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["_report_submitted"]

    @pytest.mark.parametrize(
        ("criterion", "detail"),
        [
            ("review_rtl_bugs_done", {"issues": 0, "issue_list": []}),
            (
                "review_rtl_bugs_clean",
                {
                    "issues": 0,
                    "pending": [],
                    "resolved": [
                        {
                            "severity": "MINOR",
                            "file": "rtl/dut.sv",
                            "line": 3,
                            "summary": "intentional tradeoff",
                            "status": "waived",
                            "justification": "required by the ticket",
                        }
                    ],
                },
            ),
        ],
    )
    def test_disabled_report_still_required_for_reportable_reviews(
        self,
        tmp_path: Path,
        monkeypatch,
        criterion: str,
        detail: dict,
    ):
        from booley.config import project_config

        monkeypatch.setattr(project_config, "_CONFIG_CACHE", {"RUN_REPORT": False})
        state = _FakeState(
            criteria={criterion: _FakeCriterion(met=True, mandatory=True, detail=detail)}
        )

        verdict = self._write_state_and_check(tmp_path, state)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["_report_submitted"]

    def test_disabled_report_accepts_justified_unmet_optional(self, tmp_path: Path, monkeypatch):
        from booley.config import project_config

        monkeypatch.setattr(project_config, "_CONFIG_CACHE", {"RUN_REPORT": False})
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "mutation_score": _FakeCriterion(met=False, mandatory=False),
                "_report_submitted": _FakeCriterion(
                    met=True,
                    mandatory=False,
                    detail={"unmet_optional_criteria": ["mutation_score"]},
                ),
            }
        )

        verdict = self._write_state_and_check(tmp_path, state)

        assert verdict.disposition == "review"

    def test_report_rejected_when_later_optional_failure_is_not_justified(self, tmp_path: Path):
        state = _FakeState(
            criteria={
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
                "mutation_score": _FakeCriterion(met=False, mandatory=False),
                "_report_submitted": _FakeCriterion(
                    met=True,
                    mandatory=True,
                    detail={"unmet_optional_criteria": []},
                ),
            }
        )

        verdict = self._write_state_and_check(tmp_path, state)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["_report_submitted"]

    def test_internal_criteria_excluded_from_counts(self, tmp_path: Path):
        """Criteria starting with _ should not be counted in totals."""
        state = _FakeState(
            criteria={
                "_blocked_reason": _FakeCriterion(met=False, mandatory=False, detail={}),
                "sim_pass": _FakeCriterion(met=True, mandatory=True),
            }
        )
        verdict = self._write_state_and_check(tmp_path, state)
        assert verdict.total == 1  # only sim_pass
        assert verdict.mandatory == 1

    def _fresh_state(self, tmp_path: Path) -> tuple[Path, Path]:
        work_dir = tmp_path / "work"
        (work_dir / ".booley_project").mkdir(parents=True)
        (work_dir / ".booley_project" / "booley.toml").write_text(
            "[sources.rtl]\nsource_dirs = ['rtl']\n[sources.testbench]\nsource_dirs = ['tb']\n",
            encoding="utf-8",
        )
        (work_dir / "rtl").mkdir()
        (work_dir / "tb").mkdir()
        (work_dir / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (work_dir / "tb" / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")

        state_path = tmp_path / "logs" / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "freshness"
        state.work_dir = str(work_dir)
        state.init_criteria({"sim_pass_default": True, "_report_submitted": True})
        self._stamp_sim_pass(state, work_dir)
        state.set_criterion("_report_submitted", True)
        state.save()
        return state_path, work_dir

    @staticmethod
    def _stamp_sim_pass(state: DevelopmentState, work_dir: Path) -> None:
        state.set_criterion(
            "sim_pass_default",
            True,
            detail={
                "tests_passed": 1,
                "tests_total": 1,
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl", "tb"],
                    "fingerprint": compute_source_fingerprint(work_dir),
                },
            },
        )

    def test_fresh_sim_fingerprint_accepts(self, tmp_path: Path):
        state_path, work_dir = self._fresh_state(tmp_path)

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "review"

    def test_unrelated_core_source_disappearing_does_not_stale_sim(self, tmp_path: Path):
        """F-21: a Target stamp excludes sources from unrelated Targets."""
        work_dir = tmp_path / "work"
        (work_dir / "rtl").mkdir(parents=True)
        (work_dir / "tb").mkdir()
        (work_dir / "baselines").mkdir()
        (work_dir / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
        (work_dir / "tb" / "tb.sv").write_text("module tb; endmodule\n")
        baseline = work_dir / "baselines" / "old.sv"
        baseline.write_text("module old; endmodule\n")
        (work_dir / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::demo\n"
            "filesets:\n"
            "  rtl: {files: [rtl/dut.sv]}\n"
            "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
            "  baseline: {files: [baselines/old.sv]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb}\n"
            "  baseline: {filesets: [baseline], toplevel: old}\n",
            encoding="utf-8",
        )

        state_path = tmp_path / "logs" / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "target-freshness"
        state.work_dir = str(work_dir)
        state.init_criteria({"sim_pass_sim": True, "_report_submitted": True})
        state.set_criterion(
            "sim_pass_sim",
            True,
            detail={
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl", "tb"],
                    "target": "sim",
                    "fingerprint": compute_source_fingerprint(work_dir, target="sim"),
                }
            },
        )
        state.set_criterion("_report_submitted", True)
        state.save()
        baseline.unlink()

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "review"
        assert DevelopmentState.load(state_path).criteria["sim_pass_sim"].met is True

    def test_removed_campaign_target_marks_criterion_stale(self, tmp_path: Path):
        work_dir = tmp_path / "work"
        (work_dir / ".booley_project").mkdir(parents=True)
        (work_dir / "rtl").mkdir()
        (work_dir / "tb").mkdir()
        (work_dir / "rtl" / "dut.sv").write_text("module dut; endmodule\n")
        (work_dir / "tb" / "tb.sv").write_text("module tb; endmodule\n")
        core = work_dir / "design.core"
        core.write_text(
            "CAPI=2:\n"
            "name: ::design:0\n"
            "filesets:\n"
            "  rtl: {files: [rtl/dut.sv]}\n"
            "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb}\n",
            encoding="utf-8",
        )

        state_path = tmp_path / "logs" / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "removed-target"
        state.work_dir = str(work_dir)
        state.init_criteria({"coverage_toggle_sim": True, "_report_submitted": True})
        state.set_criterion(
            "coverage_toggle_sim",
            True,
            detail={
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl", "tb", "campaign"],
                    "target": "sim",
                    "fingerprint": compute_source_fingerprint(work_dir, target="sim"),
                }
            },
        )
        state.set_criterion("_report_submitted", True)
        state.save()
        core.write_text(core.read_text().replace("  sim:", "  renamed:"), encoding="utf-8")

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["coverage_toggle_sim"]
        state = DevelopmentState.load(state_path)
        entry = state.criteria["coverage_toggle_sim"]
        assert entry.stale is True
        assert "can no longer be resolved" in entry.detail["stale_reason"]
        assert entry.detail["stale_source_categories"] == ["campaign", "rtl", "tb"]
        assert state.criteria["_report_submitted"].met is False

    def test_missing_verification_fingerprint_is_stale_unmet(self, tmp_path: Path):
        state_path, work_dir = self._fresh_state(tmp_path)
        state = DevelopmentState.load(state_path)
        state.set_criterion("sim_pass_default", True, detail={"tests_passed": 1})
        state.set_criterion("_report_submitted", True)
        state.save()

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["sim_pass_default"]
        state = DevelopmentState.load(state_path)
        assert state.criteria["sim_pass_default"].stale is True
        assert "no source fingerprint" in state.criteria["sim_pass_default"].detail["stale_reason"]

    def test_direct_rtl_edit_makes_sim_stale_unmet(self, tmp_path: Path):
        state_path, work_dir = self._fresh_state(tmp_path)
        (work_dir / "rtl" / "dut.sv").write_text(
            "module dut; wire x; endmodule\n", encoding="utf-8"
        )

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["sim_pass_default"]
        state = DevelopmentState.load(state_path)
        assert state.criteria["sim_pass_default"].met is False
        assert state.criteria["sim_pass_default"].stale is True
        assert state.criteria["sim_pass_default"].detail["stale_source_categories"] == ["rtl"]
        assert state.criteria["_report_submitted"].met is False
        assert state.criteria["_report_submitted"].stale is True

    def test_direct_testbench_edit_makes_sim_stale_unmet(self, tmp_path: Path):
        state_path, work_dir = self._fresh_state(tmp_path)
        (work_dir / "tb" / "tb.sv").write_text("module tb; wire y; endmodule\n", encoding="utf-8")

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == ["sim_pass_default"]
        state = DevelopmentState.load(state_path)
        assert state.criteria["sim_pass_default"].detail["stale_source_categories"] == ["tb"]

    def test_changed_declared_cycle_workload_hook_is_stale_unmet(self, tmp_path: Path):
        work_dir = tmp_path / "work"
        project_dir = work_dir / ".booley_project"
        (work_dir / "rtl").mkdir(parents=True)
        (work_dir / "tb").mkdir()
        (work_dir / "tools").mkdir()
        (work_dir / "rtl" / "dut.sv").write_text("module dut; endmodule\n", encoding="utf-8")
        (work_dir / "tb" / "tb.sv").write_text("module tb; endmodule\n", encoding="utf-8")
        hook = work_dir / "tools" / "prepare.py"
        hook.write_text("print('baseline')\n", encoding="utf-8")
        (work_dir / "design.core").write_text(
            "CAPI=2:\n"
            "name: ::design:0\n"
            "filesets:\n"
            "  rtl: {files: [rtl/dut.sv]}\n"
            "  tb: {files: [tb/tb.sv], tags: [tb]}\n"
            "targets:\n"
            "  sim: {filesets: [rtl, tb], toplevel: tb}\n",
            encoding="utf-8",
        )
        project_dir.mkdir()
        (project_dir / "booley.toml").write_text(
            '[flows.sim]\npre_run_commands = ["python tools/prepare.py"]\n',
            encoding="utf-8",
        )
        key = cycle_count_criterion_key("sim", "smoke")
        state_path = tmp_path / "logs" / "booley_state.json"
        state = DevelopmentState.load(state_path)
        state.slug = "cycle-workload-freshness"
        state.work_dir = str(work_dir)
        state.init_criteria(
            {key: True, "_report_submitted": True},
            criterion_params={key: {"target": "sim", "test": "smoke", "cycle_count_max": 10}},
        )
        state.set_criterion(
            key,
            True,
            detail={
                "cycles": 5,
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl", "tb", "campaign", "workload"],
                    "target": "sim",
                    "fingerprint": compute_source_fingerprint(work_dir, target="sim"),
                },
            },
        )
        state.set_criterion("_report_submitted", True)
        state.save()
        hook.write_text("print('changed')\n", encoding="utf-8")

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == [key]
        entry = DevelopmentState.load(state_path).criteria[key]
        assert entry.stale is True
        assert entry.detail["stale_source_categories"] == ["workload"]

    @pytest.mark.parametrize(
        ("criterion", "changed_path", "category"),
        [
            ("review_rtl_bugs_done", "rtl/dut.sv", "rtl"),
            ("review_tb_quality_clean", "tb/tb.sv", "tb"),
        ],
    )
    def test_review_evidence_becomes_stale_after_relevant_source_edit(
        self,
        tmp_path: Path,
        criterion: str,
        changed_path: str,
        category: str,
    ):
        state_path, work_dir = self._fresh_state(tmp_path)
        state = DevelopmentState.load(state_path)
        state.criteria.pop("sim_pass_default")
        state.init_criteria({criterion: True})
        state.set_criterion(
            criterion,
            True,
            detail={
                "issues": 0,
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": [category],
                    "fingerprint": compute_source_fingerprint(work_dir),
                },
            },
        )
        state.set_criterion("_report_submitted", True)
        state.save()
        (work_dir / changed_path).write_text("module changed; endmodule\n", encoding="utf-8")

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "failed"
        assert verdict.unmet_mandatory == [criterion]
        entry = DevelopmentState.load(state_path).criteria[criterion]
        assert entry.stale is True
        assert entry.detail["stale_source_categories"] == [category]

    def test_optional_review_evidence_becomes_stale_after_source_edit(self, tmp_path: Path):
        state_path, work_dir = self._fresh_state(tmp_path)
        state = DevelopmentState.load(state_path)
        state.init_criteria({"review_rtl_bugs_done": False})
        state.set_criterion(
            "review_rtl_bugs_done",
            True,
            detail={
                "issues": 0,
                SOURCE_FINGERPRINT_DETAIL_KEY: {
                    "categories": ["rtl"],
                    "fingerprint": compute_source_fingerprint(work_dir),
                },
            },
        )
        state.save()
        (work_dir / "rtl" / "dut.sv").write_text(
            "module changed; endmodule\n",
            encoding="utf-8",
        )

        stale = refresh_verification_freshness(state, work_dir=work_dir)

        assert stale == ["review_rtl_bugs_done"]
        entry = state.criteria["review_rtl_bugs_done"]
        assert entry.mandatory is False
        assert entry.met is False
        assert entry.stale is True

    def test_rerun_sim_refreshes_stale_criterion(self, tmp_path: Path):
        state_path, work_dir = self._fresh_state(tmp_path)
        (work_dir / "rtl" / "dut.sv").write_text(
            "module dut; wire x; endmodule\n", encoding="utf-8"
        )
        assert check_criteria_acceptance(state_path, work_dir=work_dir).disposition == "failed"

        state = DevelopmentState.load(state_path)
        self._stamp_sim_pass(state, work_dir)
        state.set_criterion("_report_submitted", True)
        state.save()

        verdict = check_criteria_acceptance(state_path, work_dir=work_dir)

        assert verdict.disposition == "review"


# ---------------------------------------------------------------------------
# format_criteria_verdict
# ---------------------------------------------------------------------------


class TestFormatCriteriaVerdict:
    def test_review_disposition(self):
        v = CriteriaVerdict(
            disposition="review",
            total=3,
            met=3,
            mandatory=2,
            mandatory_met=2,
        )
        result = format_criteria_verdict(v)
        assert "REVIEW" in result
        assert "3/3 met" in result

    def test_failed_disposition(self):
        v = CriteriaVerdict(
            disposition="failed",
            total=3,
            met=1,
            mandatory=2,
            mandatory_met=1,
            unmet_mandatory=["sim_pass"],
        )
        result = format_criteria_verdict(v)
        assert "FAILED" in result
        assert "sim_pass" in result

    def test_blocked_disposition(self):
        v = CriteriaVerdict(
            disposition="blocked",
            blocked_reason="human required",
        )
        result = format_criteria_verdict(v)
        assert "BLOCKED" in result
        assert "human required" in result


# ---------------------------------------------------------------------------
# build_criteria_summary_lines — icon states and group collapsing
# ---------------------------------------------------------------------------


class TestBuildCriteriaSummaryLines:
    def _build(self, tmp_path: Path, criteria: dict) -> list[str]:
        state_path = tmp_path / "booley_state.json"
        state_path.write_text("{}", encoding="utf-8")
        state = _FakeState(criteria=criteria)
        with patch("booley.dev_support.development_state.DevelopmentState") as mock_cls:
            mock_cls.load.return_value = state
            lines, _ = build_criteria_summary_lines(state_path)
        return lines

    @staticmethod
    def _strip(text: str) -> str:
        return _ANSI_RE.sub("", text)

    def test_not_run_criterion_shows_open_circle(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "sim_pass": _FakeCriterion(met=False, mandatory=True),
            },
        )
        raw = self._strip(lines[0])
        assert raw.startswith("○ ")
        assert "sim_pass" in raw

    def test_failed_criterion_shows_cross(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "sim_pass": _FakeCriterion(
                    met=False,
                    mandatory=True,
                    detail={"exit_code": 1},
                ),
            },
        )
        raw = self._strip(lines[0])
        assert raw.startswith("✗ ")

    def test_met_criterion_shows_check(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "lint_clean": _FakeCriterion(met=True, mandatory=True),
            },
        )
        raw = self._strip(lines[0])
        assert raw.startswith("✓ ")

    def test_stale_criterion_shows_refresh(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "sim_pass": _FakeCriterion(
                    met=False,
                    mandatory=True,
                    detail={"exit_code": 0},
                    stale=True,
                    ever_met=True,
                ),
            },
        )
        raw = self._strip(lines[0])
        assert raw.startswith("↻ ")

    def test_coverage_group_collapses_when_all_pending(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "coverage_toggle": _FakeCriterion(met=False, mandatory=True),
                "coverage_branch": _FakeCriterion(met=False, mandatory=True),
                "coverage_fsm": _FakeCriterion(met=False, mandatory=True),
            },
        )
        assert len(lines) == 1
        raw = self._strip(lines[0])
        assert "coverage" in raw
        assert "not yet run" in raw

    def test_coverage_group_expands_when_any_evaluated(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "coverage_toggle": _FakeCriterion(
                    met=False,
                    mandatory=True,
                    detail={"toggle": {"pct": 42}},
                    params={"min_pct": 80},
                ),
                "coverage_branch": _FakeCriterion(met=False, mandatory=True),
                "coverage_fsm": _FakeCriterion(met=True, mandatory=True),
            },
        )
        raw_all = [self._strip(ln) for ln in lines if ln]
        names = [r for r in raw_all if "coverage_" in r]
        assert len(names) == 3

    def test_met_after_not_met_with_separator(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "sim_pass": _FakeCriterion(met=False, mandatory=True),
                "lint_clean": _FakeCriterion(met=True, mandatory=True),
            },
        )
        assert "" in lines
        sim_idx = next(i for i, ln in enumerate(lines) if "sim_pass" in self._strip(ln))
        lint_idx = next(i for i, ln in enumerate(lines) if "lint_clean" in self._strip(ln))
        assert sim_idx < lint_idx

    def test_review_group_collapses_when_all_pending(self, tmp_path: Path):
        lines = self._build(
            tmp_path,
            {
                "review_rtl_bugs": _FakeCriterion(met=False, mandatory=True),
                "review_tb_quality": _FakeCriterion(met=False, mandatory=True),
            },
        )
        assert len(lines) == 1
        raw = self._strip(lines[0])
        assert "reviews" in raw
        assert "not yet run" in raw
