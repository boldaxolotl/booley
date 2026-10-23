"""A Target reads the one ``tests.toml`` section that names it, never another core's."""

from pathlib import Path

import pytest

from booley.config.project_config import load_test_configuration_field, lookup_target_section

A = "vendor:a:x:1.0#sim"
B = "vendor:b:y:1.0#sim"
# Every way to write the section of core B / core A's `sim` Target.
B_KEYS = ("y#sim", "b:y#sim", "vendor:b:y#sim", B)
A_KEYS = ("x#sim", "a:x#sim", "vendor:a:x#sim", A)


def test_each_core_gets_its_own_section_for_every_key_form_and_order():
    for a_key in A_KEYS:
        for b_key in B_KEYS:
            pairs = [(a_key, ["a_test"]), (b_key, ["b_test"])]
            for ordered in (pairs, pairs[::-1]):
                sections = dict(ordered)
                assert lookup_target_section(sections, A) == ["a_test"], (a_key, b_key)
                assert lookup_target_section(sections, B) == ["b_test"], (a_key, b_key)


def test_qualified_key_is_never_served_to_another_core_and_bare_key_is_the_default():
    pairs = [("vendor:a:x#sim", ["a_test"]), ("sim", ["default_test"])]
    for ordered in (pairs, pairs[::-1]):
        sections = dict(ordered)
        assert lookup_target_section(sections, A) == ["a_test"]
        assert lookup_target_section(sections, B) == ["default_test"]
    assert lookup_target_section({"vendor:a:x#sim": ["a_test"]}, B) is None


def test_two_keys_for_one_target_are_refused_naming_both():
    sections = {"y#sim": ["one"], "vendor:b:y#sim": ["two"]}
    with pytest.raises(ValueError, match=r"\[vendor:b:y#sim\] and \[y#sim\]"):
        lookup_target_section(sections, B)
    assert lookup_target_section(sections, A) is None


def test_every_field_comes_from_the_one_section_of_the_target(tmp_path: Path):
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text("[project]\n", encoding="utf-8")
    (booley_dir / "tests.toml").write_text(
        '["vendor:a:x#sim"]\ntests = ["a_test"]\n'
        '[sim]\ntests = ["b_test"]\nskip = ["b_test"]\nenv = { WHO = "default" }\n',
        encoding="utf-8",
    )
    fields = {
        name: load_test_configuration_field(tmp_path, name) for name in ("tests", "skip", "env")
    }
    # Core A's own section declares only `tests`: it inherits nothing from [sim].
    assert lookup_target_section(fields["tests"], A) == ["a_test"]
    assert lookup_target_section(fields["skip"], A) is None
    assert lookup_target_section(fields["env"], A) is None
    assert lookup_target_section(fields["skip"], B) == ["b_test"]
    assert lookup_target_section(fields["env"], B) == {"WHO": "default"}
