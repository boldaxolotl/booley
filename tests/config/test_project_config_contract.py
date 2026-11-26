"""Project names, shared lists, selectors, and environment tables."""

import logging
import tomllib
from pathlib import Path

import pytest

from booley.config import project_config
from booley.config.project_config import (
    load_test_configuration,
    normalize_configs_toml,
    normalize_tests_toml,
    project_name,
)


def _write_toml(work_dir: Path, body: str) -> None:
    project = work_dir / ".booley"
    project.mkdir(parents=True, exist_ok=True)
    (project / "booley.toml").write_text(body, encoding="utf-8")


class TestProjectName:
    @pytest.fixture(autouse=True)
    def empty_ambient_project(self, tmp_path, monkeypatch):
        """The default-name contract assumes no intentional legacy ambient Project override."""
        monkeypatch.delenv("BOOLEY_DIR", raising=False)
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


def test_configs_toml_resolves_shared_test_list():
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


@pytest.mark.parametrize(
    ("normalize", "section", "match"),
    [
        (normalize_configs_toml, {"defines": [], "test_list": "missing"}, "unknown test list"),
        (normalize_tests_toml, {"test_list": "missing"}, "unknown test list"),
        (
            normalize_configs_toml,
            {"defines": [], "tests": ["smoke"], "test_list": "rv_tests"},
            "both tests and test_list",
        ),
        (
            normalize_tests_toml,
            {"tests": ["smoke"], "test_list": "rv_tests"},
            "both tests and test_list",
        ),
        (normalize_tests_toml, {"tests": [1, 2]}, "must be list"),
    ],
    ids=["configs-unknown", "tests-unknown", "configs-both", "tests-both", "non-string"],
)
def test_shared_test_list_refusals(normalize, section, match):
    with pytest.raises(ValueError, match=match):
        normalize({"test_lists": {"rv_tests": ["smoke"]}, "fast": section})


class TestTestsTomlNameRules:
    """Test names obey the Test Run name rules (ADR 0072)."""

    def test_valid_file_with_every_key_loads_unchanged(self):
        raw = {
            "test_lists": {"smoke": ["reset", "slow-first", "tb/basic.v2"]},
            "sim": {
                "tests": ["reset", "basic", "Default", "x5"],
                "skip": ["basic"],
                "select": "+test={name}",
                "env": {"FLAVOR": "small", "BOOLEYISH": "b"},
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

    def test_rejects_invalid_name_in_shared_test_list(self):
        # An unreferenced list is refused too: the file states a Test name.
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"test_lists": {"nightly": ["soak@7"]}, "sim": {}})
        message = str(excinfo.value)
        assert message.startswith("tests.toml [test_lists].nightly: Test name 'soak@7'")
        assert "'@'" in message

    @pytest.mark.parametrize("key", ["tests", "skip"])
    def test_rejects_duplicate_test_in_one_target(self, key: str):
        section = {"tests": ["reset", "basic"], key: ["reset", "basic", "reset"]}
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"sim": section})
        assert str(excinfo.value) == (
            f"tests.toml [sim].{key} names Test 'reset' more than once; each "
            "Test name must be unique within a Target"
        )

    def test_duplicate_from_a_shared_list_names_the_list_not_a_tests_key(self):
        # The section has no `tests` key: the duplicate is written in the list.
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"test_lists": {"dup": ["a", "a"]}, "sim": {"test_list": "dup"}})
        message = str(excinfo.value)
        assert message == (
            "tests.toml [sim].test_list = 'dup' ([test_lists].dup) names Test 'a' "
            "more than once; each Test name must be unique within a Target"
        )
        assert "[sim].tests" not in message

    def test_explicit_checkout_read_path_applies_the_rules(self, tmp_path: Path):
        project = tmp_path / ".booley"
        project.mkdir()
        (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (project / "tests.toml").write_text('[sim]\ntests = ["default"]\n', encoding="utf-8")
        with pytest.raises(ValueError, match=r"tests\.toml \[sim\]\.tests: .*reserved"):
            load_test_configuration(tmp_path)

    def test_active_project_read_path_applies_the_rules(self, tmp_path: Path, monkeypatch):
        project = tmp_path / ".booley"
        project.mkdir()
        (project / "booley.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8")
        (project / "tests.toml").write_text('[sim]\ntests = ["a+b"]\n', encoding="utf-8")
        monkeypatch.setenv("BOOLEY_DIR", str(project))
        monkeypatch.chdir(tmp_path)
        # private: no public function empties the memoized Project configuration
        monkeypatch.setattr(project_config, "_CONFIG_CACHE", None)
        reset_cache()
        try:
            with pytest.raises(ValueError, match=r"tests\.toml \[sim\]\.tests: .*'\+'"):
                _ = project_config.TEST_NAMES
        finally:
            reset_cache()


class TestTestsTomlClosedKeySet:
    """A Target section holds only keys the Simulation Flow applies."""

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("variant", {"stress": {"default": "short", "short": ["+iters=1000"]}}),
            ("seeds", [1, 2]),
            ("plusargs", ["+a"]),
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
            "only: tests, test_list, skip, select, env, seed, default_seed, variants"
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


def _section(**keys: object) -> dict[str, object]:
    return {"sim": {"tests": ["reset", "stress"], **keys}}


_VARIANTS = {
    "stress": {
        "default": "short",
        "short": ["+iters=1000"],
        "fault": {"args": ["+err_rate=5"], "env": {"FAULT_MODE": "1"}},
    }
}


class TestTestsTomlVariantsAndSeeds:
    """`variants`, `seed` and `default_seed` load, with every refusal (ADR 0072)."""

    def test_accepts_the_adr_example_and_a_getopt_seed_and_an_empty_default_variant(self):
        raw = _section(
            select="+test_id={name}", seed="+seed={seed}", default_seed=7, variants=_VARIANTS
        )
        section = normalize_tests_toml(raw)["sim"]
        assert section["seed"] == "+seed={seed}"
        assert section["default_seed"] == 7
        assert section["variants"] == _VARIANTS
        assert (
            normalize_tests_toml(_section(seed="--seed={seed}"))["sim"]["seed"] == "--seed={seed}"
        )
        plain = {"stress": {"default": "plain", "plain": []}}
        assert normalize_tests_toml(_section(variants=plain))["sim"]["variants"] == plain

    @pytest.mark.parametrize(
        ("variants", "message"),
        [
            (
                {"ghost": {"default": "a", "a": []}},
                "tests.toml [sim].variants.ghost declares Test Variants for 'ghost', which "
                "is not one of the Target's tests (reset, stress)",
            ),
            (
                {"stress": {"a": []}},
                "tests.toml [sim].variants.stress must name its default Test Variant: "
                'add default = "<variant>"',
            ),
            (
                {"stress": {"default": "b", "a": []}},
                "tests.toml [sim].variants.stress.default names 'b', which is not a "
                "declared Test Variant (declared: a)",
            ),
            (
                {"stress": {"default": "a", "a": ["iters=1"]}},
                "tests.toml [sim].variants.stress.a argument 'iters=1' must start with '+' or '-'",
            ),
            (
                {"stress": {"default": "a", "a": ["+a b"]}},
                "tests.toml [sim].variants.stress.a argument '+a b' must be one token "
                "without whitespace or NUL; declare each argument as its own list entry",
            ),
            (
                {"stress": {"default": "a", "a": {"args": [], "plusargs": []}}},
                "tests.toml [sim].variants.stress.a has unknown key 'plusargs'; a Test "
                "Variant table holds only args and env",
            ),
            (
                {"stress": {"default": "a", "a": ["+test_id=3"]}},
                "tests.toml [sim].variants.stress.a argument '+test_id=3' is not "
                "allowed: the select template sets it",
            ),
            (
                {"stress": {"default": "a", "a": ["+verilator+seed+5"]}},
                "tests.toml [sim].variants.stress.a argument '+verilator+seed+5' is not "
                "allowed: Booley passes the Verilator seed option itself",
            ),
            (
                {"stress": {"default": "a", "a": {"env": {"FLAVOR": "x"}}}},
                "tests.toml [sim].variants.stress.a.env may not set FLAVOR: the Target's "
                "env table already sets it, and the Target owns that value",
            ),
        ],
    )
    def test_variant_refusals(self, variants: dict, message: str):
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml(_section(env={"FLAVOR": "base"}, variants=variants))
        assert str(excinfo.value) == message

    def test_variant_may_not_repeat_the_seed_option(self):
        variants = {"stress": {"default": "a", "a": ["+seed=9"]}}
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml(_section(seed="+seed={seed}", variants=variants))
        assert str(excinfo.value) == (
            "tests.toml [sim].variants.stress.a argument '+seed=9' is not allowed: "
            "the seed template sets it"
        )

    def test_variant_name_obeys_the_test_name_rule(self):
        variants = {"stress": {"default": "a", "a": [], "b+c": []}}
        with pytest.raises(ValueError, match=r"variants\.stress: Test Variant name 'b\+c'"):
            normalize_tests_toml(_section(variants=variants))

    def test_target_without_tests_cannot_have_variants(self):
        with pytest.raises(ValueError, match=r"not one of the Target's tests \(none declared\)"):
            normalize_tests_toml({"sim": {"variants": {"x": {"default": "a", "a": []}}}})

    @pytest.mark.parametrize(
        "name",
        [
            "BOOLEY_TARGET",
            "COCOTB_RANDOM_SEED",
            "COCOTB_TEST_FILTER",
            "MODULE",
            "TESTCASE",
            "TOPLEVEL",
            "TOPLEVEL_LANG",
            "RANDOM_SEED",
            "LIBPYTHON_LOC",
            "PYGPI_PYTHON_BIN",
            "GPI_USERS",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
        ],
    )
    def test_reserved_environment_names_are_refused_in_both_tables(self, name: str):
        variants = {"stress": {"default": "a", "a": {"env": {name: "1"}}}}
        with pytest.raises(ValueError, match=rf"variants\.stress\.a\.env may not set {name}: "):
            normalize_tests_toml(_section(variants=variants))
        with pytest.raises(ValueError, match=rf"tests\.toml \[sim\]\.env may not set {name}: "):
            normalize_tests_toml(_section(env={name: "1"}))

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            ("", "tests.toml [sim].seed must be a non-empty string"),
            (
                "+seed=1",
                "tests.toml [sim].seed template '+seed=1' must contain the plain {seed} "
                "field exactly once and no other field, conversion or format",
            ),
            (
                "+a={seed}+b={seed}",
                "tests.toml [sim].seed template '+a={seed}+b={seed}' must contain the "
                "plain {seed} field exactly once and no other field, conversion or format",
            ),
            (
                "+seed={seed}{name}",
                "tests.toml [sim].seed template '+seed={seed}{name}' must contain the "
                "plain {seed} field exactly once and no other field, conversion or format",
            ),
            (
                "+seed={{seed}}",
                "tests.toml [sim].seed template '+seed={{seed}}' must contain the "
                "plain {seed} field exactly once and no other field, conversion or format",
            ),
            (
                "+seed {seed}",
                "tests.toml [sim].seed template '+seed {seed}' must be one token without "
                "whitespace or NUL; declare each argument as its own list entry",
            ),
            (
                "seed={seed}",
                "tests.toml [sim].seed template 'seed={seed}' must start with '+' or '-'",
            ),
            (
                "+verilator+seed+{seed}",
                "tests.toml [sim].seed may not be +verilator+seed+: Booley passes the "
                "Verilator seed option itself",
            ),
            (
                "+test_id={seed}",
                "tests.toml [sim].seed sets the same option as the select template "
                "(+test_id); the seed needs its own option",
            ),
        ],
    )
    def test_seed_template_refusals(self, template: str, message: str):
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml(_section(seed=template))
        assert str(excinfo.value) == message

    @pytest.mark.parametrize("value", [1, 7, 2**31 - 1])
    def test_default_seed_accepts_the_whole_range(self, value: int):
        assert normalize_tests_toml(_section(default_seed=value))["sim"]["default_seed"] == value

    @pytest.mark.parametrize("value", [0, -1, 2**31, True, "7", 1.0])
    def test_default_seed_refusals(self, value: object):
        with pytest.raises(ValueError, match=r"tests\.toml \[sim\]\.default_seed"):
            normalize_tests_toml(_section(default_seed=value))


class TestShippedTestsTemplate:
    """The annotated template the setup skill reads must itself load."""

    _TEMPLATE = (
        Path(project_config.__file__).parents[1]
        / "data"
        / "skills"
        / "booley-setup"
        / "TESTS_TEMPLATE.toml"
    )

    def test_template_loads_as_written(self):
        sections = normalize_tests_toml(tomllib.loads(self._TEMPLATE.read_text(encoding="utf-8")))
        assert sections["sim_dut"]["tests"] == ["reset", "basic", "stress"]

    def test_commented_seed_and_variant_examples_load_when_enabled(self):
        text = self._TEMPLATE.read_text(encoding="utf-8")
        for example in (
            '# seed = "+seed={seed}"',
            "# default_seed = 7",
            "# [sim_dut.variants.stress]",
            '# default = "short"',
            '# short = ["+iters=1000"]',
            '# soak  = ["+iters=100000"]',
            '# fault = { args = ["+err_rate=5"], env = { FAULT_MODE = "1" } }',
        ):
            assert text.count(example) == 1, example
            text = text.replace(example, example.removeprefix("# "))
        # The Variant table must follow the [sim_dut] keys it belongs to.
        head, variants = text.split("[sim_dut.variants.stress]")
        table, rest = variants.split("[sim_fast]")
        sections = normalize_tests_toml(
            tomllib.loads(head + "[sim_fast]" + rest + "[sim_dut.variants.stress]" + table)
        )
        assert sections["sim_dut"]["seed"] == "+seed={seed}"
        assert sections["sim_dut"]["default_seed"] == 7
        assert sorted(sections["sim_dut"]["variants"]["stress"]) == [
            "default",
            "fault",
            "short",
            "soak",
        ]


class TestSelectTemplateValidation:
    """`select` must be exactly one well-formed option token (decision 16)."""

    @pytest.mark.parametrize(
        "template",
        [
            "+test_id={index}",
            "+test={name}",
            "+TESTID={index}",
            # SETUP-7: a getopt long option (a CPU core's --meminit) is a valid
            # single-token selector; no plusarg can express it.
            "--meminit=ram,{name}",
            "--test={index}",
            "-t{index}",
        ],
    )
    def test_select_template_accepts_one_option_token(self, template: str):
        assert normalize_tests_toml({"k": {"select": template}})["k"]["select"] == template

    @pytest.mark.parametrize(
        ("template", "match"),
        [
            ("test_id={index}", "exactly one option token"),
            ("+a={index} +b=1", "exactly one option token"),
            ("--meminit=ram,{name} --extra", "exactly one option token"),
            ("+t={bogus}", "unknown field"),
            ("  ", "non-empty string"),
        ],
    )
    def test_select_template_refusals(self, template: str, match: str):
        with pytest.raises(ValueError, match=match):
            normalize_tests_toml({"k": {"select": template}})


class TestEnvTableValidation:
    """tests.toml `env` — the per-Target simulator environment (F-5)."""

    @pytest.mark.parametrize("name", ["X\n", "FLAVOR\n", "\nX"])
    def test_rejects_name_with_a_newline(self, name: str):
        # Regression: `$` with `.match()` accepted a trailing newline.
        with pytest.raises(ValueError) as excinfo:
            normalize_tests_toml({"k": {"env": {name: "x"}}})
        assert str(excinfo.value) == (
            f"tests.toml [k].env key {name!r} is not a valid environment variable "
            "name ([A-Za-z_][A-Za-z0-9_]*)"
        )


from booley.runtime.booley_dir import reset_cache
