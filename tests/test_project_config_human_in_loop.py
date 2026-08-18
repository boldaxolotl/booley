"""Tests for booley.project_config.is_human_in_loop."""

from __future__ import annotations

from pathlib import Path

import pytest

from booley.project_config import (
    DEFAULT_TEST_SELECT,
    is_human_in_loop,
    normalize_configs_toml,
    normalize_tests_toml,
    render_test_selector,
)


def _write_toml(work_dir: Path, body: str) -> None:
    project = work_dir / ".booley_project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "booley.toml").write_text(body, encoding="utf-8")


class TestIsHumanInLoop:
    def test_default_true_when_no_toml(self, tmp_path: Path):
        """No TOML file → safe-by-default True."""
        assert is_human_in_loop(tmp_path) is True

    def test_default_true_when_section_absent(self, tmp_path: Path):
        """TOML present but [developer] missing → True."""
        _write_toml(tmp_path, '[project]\nname = "x"\n')
        assert is_human_in_loop(tmp_path) is True

    def test_default_true_when_key_absent(self, tmp_path: Path):
        """[developer] present but no human_in_the_loop key → True."""
        _write_toml(tmp_path, "[developer]\nother_key = 1\n")
        assert is_human_in_loop(tmp_path) is True

    def test_explicit_true(self, tmp_path: Path):
        _write_toml(tmp_path, "[developer]\nhuman_in_the_loop = true\n")
        assert is_human_in_loop(tmp_path) is True

    def test_explicit_false(self, tmp_path: Path):
        _write_toml(tmp_path, "[developer]\nhuman_in_the_loop = false\n")
        assert is_human_in_loop(tmp_path) is False


class TestNormalizeConfigsToml:
    def test_resolves_shared_test_list(self):
        configs = normalize_configs_toml(
            {
                "test_lists": {"rv_tests": ["smoke", "full"]},
                "fast": {"defines": [], "test_list": "rv_tests"},
                "slow": {"defines": [], "tests": ["nightly"]},
            }
        )

        assert configs == {
            "fast": {"defines": [], "tests": ["smoke", "full"]},
            "slow": {"defines": [], "tests": ["nightly"]},
        }

    def test_rejects_unknown_shared_test_list(self):
        with pytest.raises(ValueError, match="unknown test list"):
            normalize_configs_toml(
                {
                    "test_lists": {"rv_tests": ["smoke"]},
                    "fast": {"defines": [], "test_list": "missing"},
                }
            )

    def test_rejects_inline_tests_and_shared_test_list_together(self):
        with pytest.raises(ValueError, match="both tests and test_list"):
            normalize_configs_toml(
                {
                    "test_lists": {"rv_tests": ["smoke"]},
                    "fast": {
                        "defines": [],
                        "tests": ["smoke"],
                        "test_list": "rv_tests",
                    },
                }
            )

    def test_malformed_toml_falls_back_to_module_default(self, tmp_path: Path):
        """Unparseable TOML must not crash callers."""
        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "booley.toml").write_text(
            "[developer\nhuman_in_the_loop = false",  # missing closing bracket
            encoding="utf-8",
        )
        # Returns the import-time module default (True) rather than raising
        assert is_human_in_loop(tmp_path) is True

    def test_legacy_pipeline_toml_filename_accepted(self, tmp_path: Path):
        """Legacy pipeline.toml fallback resolves the same way."""
        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "pipeline.toml").write_text(
            "[developer]\nhuman_in_the_loop = false\n",
            encoding="utf-8",
        )
        assert is_human_in_loop(tmp_path) is False


class TestNormalizeTestsToml:
    """tests.toml — the per-Target test registry (ADR 0022 decisions 15/16)."""

    def test_inline_tests_and_select(self):
        result = normalize_tests_toml(
            {
                "lite": {"tests": ["smoke", "stress"], "select": "+test_id={index}"},
            }
        )
        assert result == {
            "lite": {"tests": ["smoke", "stress"], "select": "+test_id={index}"},
        }

    def test_resolves_shared_test_list(self):
        result = normalize_tests_toml(
            {
                "test_lists": {"rv_tests": ["smoke", "full"]},
                "fast": {"test_list": "rv_tests"},
                "slow": {"tests": ["nightly"]},
            }
        )
        assert result == {
            "fast": {"tests": ["smoke", "full"]},
            "slow": {"tests": ["nightly"]},
        }

    def test_target_without_tests_is_allowed(self):
        # A Target may declare only a select template (tests added later).
        assert normalize_tests_toml({"lite": {"select": "+t={index}"}}) == {
            "lite": {"select": "+t={index}"},
        }

    def test_rejects_unknown_shared_test_list(self):
        with pytest.raises(ValueError, match="unknown test list"):
            normalize_tests_toml(
                {
                    "test_lists": {"rv_tests": ["smoke"]},
                    "fast": {"test_list": "missing"},
                }
            )

    def test_rejects_inline_tests_and_shared_test_list_together(self):
        with pytest.raises(ValueError, match="both tests and test_list"):
            normalize_tests_toml(
                {
                    "test_lists": {"rv_tests": ["smoke"]},
                    "fast": {"tests": ["smoke"], "test_list": "rv_tests"},
                }
            )

    def test_rejects_non_string_tests(self):
        with pytest.raises(ValueError, match="must be list"):
            normalize_tests_toml({"lite": {"tests": [1, 2]}})


class TestSelectTemplateValidation:
    """`select` must be exactly one well-formed option token (decision 16)."""

    def test_accepts_index_and_name_fields(self):
        for template in ("+test_id={index}", "+test={name}", "+TESTID={index}"):
            assert normalize_tests_toml({"k": {"select": template}})["k"]["select"] == (template)

    def test_accepts_getopt_argument(self):
        # SETUP-7: a getopt long option (a CPU core's --meminit) is a valid
        # single-token selector — no plusarg can express it.
        for template in ("--meminit=ram,{name}", "--test={index}", "-t{index}"):
            assert normalize_tests_toml({"k": {"select": template}})["k"]["select"] == (template)

    def test_rejects_missing_leading_prefix(self):
        with pytest.raises(ValueError, match="exactly one option token"):
            normalize_tests_toml({"k": {"select": "test_id={index}"}})

    def test_rejects_multiple_tokens(self):
        with pytest.raises(ValueError, match="exactly one option token"):
            normalize_tests_toml({"k": {"select": "+a={index} +b=1"}})

    def test_rejects_multiple_getopt_tokens(self):
        with pytest.raises(ValueError, match="exactly one option token"):
            normalize_tests_toml({"k": {"select": "--meminit=ram,{name} --extra"}})

    def test_rejects_unknown_template_field(self):
        with pytest.raises(ValueError, match="unknown field"):
            normalize_tests_toml({"k": {"select": "+t={bogus}"}})

    def test_rejects_empty_template(self):
        with pytest.raises(ValueError, match="non-empty string"):
            normalize_tests_toml({"k": {"select": "  "}})


class TestRenderTestSelector:
    """`render_test_selector` renders a Target's template, default `+test_id`."""

    def test_default_template_when_target_unknown(self):
        # No tests.toml in the test project → module TEST_SELECT empty → default.
        assert render_test_selector("nope", 3, "boot") == "+test_id=3"
        assert DEFAULT_TEST_SELECT == "+test_id={index}"

    def test_renders_declared_template(self, monkeypatch):
        from booley import project_config

        monkeypatch.setitem(project_config.TEST_SELECT, "lite", "+test={name}")
        assert render_test_selector("lite", 1, "stress") == "+test=stress"


class TestEnvTableValidation:
    """tests.toml `env` — the per-Target simulator environment (F-5)."""

    def test_accepts_string_valued_table(self):
        assert normalize_tests_toml({"sim_vanilla": {"env": {"FLAVOR": "vanilla"}}}) == {
            "sim_vanilla": {"env": {"FLAVOR": "vanilla"}},
        }

    def test_coexists_with_tests_and_select(self):
        result = normalize_tests_toml(
            {"k": {"tests": ["a"], "select": "+t={index}", "env": {"FLAVOR": "small"}}}
        )
        assert result["k"]["env"] == {"FLAVOR": "small"}

    def test_rejects_non_table(self):
        with pytest.raises(ValueError, match="must be a table"):
            normalize_tests_toml({"k": {"env": ["FLAVOR=vanilla"]}})

    def test_rejects_non_string_value(self):
        # A bare TOML integer would mean something different once exported.
        with pytest.raises(ValueError, match="must be a string"):
            normalize_tests_toml({"k": {"env": {"WIDTH": 32}}})

    def test_rejects_non_identifier_name(self):
        with pytest.raises(ValueError, match="not a valid environment"):
            normalize_tests_toml({"k": {"env": {"MY-VAR": "x"}}})
