"""Project names, shared lists, selectors, and environment tables."""

import logging
from pathlib import Path

import pytest

from booley.config import project_config
from booley.config.project_config import (
    DEFAULT_TEST_SELECT,
    TESTS_SECTION_KEYS,
    load_test_configuration,
    normalize_configs_toml,
    normalize_tests_toml,
    project_name,
    render_test_selector,
)


def _write_toml(work_dir: Path, body: str) -> None:
    project = work_dir / ".booley_project"
    project.mkdir(parents=True, exist_ok=True)
    (project / "booley.toml").write_text(body, encoding="utf-8")


class TestProjectName:
    @pytest.fixture(autouse=True)
    def empty_ambient_project(self, tmp_path, monkeypatch):
        """The default-name contract assumes no intentional legacy ambient Project override."""
        monkeypatch.delenv("BOOLEY_PROJECT_DIR", raising=False)
        monkeypatch.chdir(tmp_path)
        reset_cache()
        yield
        reset_cache()

    def test_missing_toml_uses_default_without_warning(self, tmp_path: Path, caplog):
        assert project_name(tmp_path) == "rtl_project"
        assert not caplog.records

    def test_malformed_toml_logs_checkout_context(self, tmp_path: Path, caplog):
        _write_toml(tmp_path, "[project\nname = 'broken'")

        with caplog.at_level(logging.WARNING, logger="booley.config.project_config"):
            assert project_name(tmp_path) == "rtl_project"

        assert str(tmp_path) in caplog.text
        assert "could not read Project name" in caplog.text


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


class TestTestsTomlNameRules:
    """Test names obey the Test Run name rules (ADR 0072)."""

    def test_valid_file_with_every_key_loads_unchanged(self):
        raw = {
            "test_lists": {"smoke": ["reset", "slow-first", "tb/basic.v2"]},
            "sim": {
                "tests": ["reset", "basic", "Default", "x5"],
                "skip": ["basic"],
                "select": "+test={name}",
                "env": {"FLAVOR": "small"},
            },
            "sim_fast": {"test_list": "smoke"},
            "lib:ip:port#sim": {"tests": ["reset"]},
            "sim_empty": {},
        }
        assert normalize_tests_toml(raw) == {
            "sim": raw["sim"],
            "sim_fast": {"tests": ["reset", "slow-first", "tb/basic.v2"]},
            "lib:ip:port#sim": {"tests": ["reset"]},
            "sim_empty": {},
        }

    @pytest.mark.parametrize(
        ("name", "reason"),
        [
            ("", "must not be empty"),
            ("my test", "whitespace"),
            ("smoke+fast", "'+'"),
            ("smoke@1", "'@'"),
            ("it's", "single quote"),
            ('say"hi', "double quote"),
            ("a\\b", "backslash"),
            ("bell\x07", "control character"),
            ("default", "reserved"),
        ],
    )
    @pytest.mark.parametrize("key", ["tests", "skip"])
    def test_rejects_invalid_test_name(self, key: str, name: str, reason: str):
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"sim": {key: ["reset", name]}})
        message = str(excinfo.value)
        assert message.startswith(f"tests.toml [sim].{key}: Test name ")
        assert reason in message

    def test_rejects_invalid_name_in_shared_test_list(self):
        # An unreferenced list is refused too: the file states a Test name.
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"test_lists": {"nightly": ["soak@7"]}, "sim": {}})
        message = str(excinfo.value)
        assert message.startswith("tests.toml [test_lists].nightly: Test name 'soak@7'")
        assert "'@'" in message

    def test_rejects_duplicate_test_in_one_target(self):
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"sim": {"tests": ["reset", "basic", "reset"]}})
        message = str(excinfo.value)
        assert message.startswith("tests.toml [sim].tests registers Test 'reset' more than once")

    def test_rejects_duplicate_test_from_shared_list(self):
        with pytest.raises(ValueError, match=r"\[sim\]\.tests registers Test 'a' more than once"):
            normalize_tests_toml({"test_lists": {"dup": ["a", "a"]}, "sim": {"test_list": "dup"}})

    def test_same_test_name_in_two_targets_is_allowed(self):
        raw = {"sim_a": {"tests": ["reset"]}, "sim_b": {"tests": ["reset"]}}
        assert normalize_tests_toml(raw) == raw

    def test_explicit_checkout_read_path_applies_the_rules(self, tmp_path: Path):
        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (project / "tests.toml").write_text('[sim]\ntests = ["default"]\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"tests\.toml \[sim\]\.tests: .*reserved"):
            load_test_configuration(tmp_path)

    def test_active_project_read_path_applies_the_rules(self, tmp_path: Path, monkeypatch):
        project = tmp_path / ".booley_project"
        project.mkdir()
        (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (project / "tests.toml").write_text('[sim]\ntests = ["a+b"]\n', encoding="utf-8")
        monkeypatch.setenv("BOOLEY_PROJECT_DIR", str(project))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(project_config, "_CONFIG_CACHE", None)
        reset_cache()
        try:
            with pytest.raises(ValueError, match=r"tests\.toml \[sim\]\.tests: .*'\+'"):
                _ = project_config.TEST_NAMES
        finally:
            reset_cache()


class TestTestsTomlClosedKeySet:
    """A Target section holds only keys the Simulation Flow applies."""

    def test_section_keys_are_exactly_the_documented_set(self):
        assert TESTS_SECTION_KEYS == ("tests", "test_list", "skip", "select", "env")

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("variants", {"stress": {"default": "short", "short": ["+iters=1000"]}}),
            ("seed", "+seed={seed}"),
            ("default_seed", 7),
            ("test", ["reset"]),
            ("Tests", ["reset"]),
            ("timeout", 30),
        ],
    )
    def test_rejects_unknown_section_key(self, key: str, value: object):
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"sim": {"tests": ["reset", "stress"], key: value}})
        assert str(excinfo.value) == (
            f"tests.toml [sim] has unknown key {key!r}; a Target section holds "
            "only: tests, test_list, skip, select, env"
        )

    def test_unknown_key_names_the_qualified_target_section(self):
        with pytest.raises(ValueError, match=r"tests\.toml \[lib:ip:port#sim\] has unknown key"):
            normalize_tests_toml({"lib:ip:port#sim": {"plusargs": ["+a"]}})

    def test_legacy_configs_toml_keeps_free_form_sections(self):
        raw = {
            "test_lists": {"any": ["a+b", "default"]},
            "default": {"defines": [], "test_list": "any", "tb_top": "tb"},
        }
        assert normalize_configs_toml(raw) == {
            "default": {"defines": [], "tb_top": "tb", "tests": ["a+b", "default"]},
        }


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
        from booley.config import project_config

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


from booley.runtime.project_dir import reset_cache
