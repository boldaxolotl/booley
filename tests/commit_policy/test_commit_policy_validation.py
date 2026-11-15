"""Behavior and identity contracts for the shared commit-policy owner."""

from pathlib import Path

import pytest

from booley.commit_policy import StealthPolicy, policy, validate_message


def _configured_project(tmp_path: Path, stealth: str) -> Path:
    booley_dir = tmp_path / ".booley"
    booley_dir.mkdir()
    (booley_dir / "booley.toml").write_text(f"[stealth]\n{stealth}", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    ("message", "error_fragment"),
    [
        ("fix(core): clean summary\n", None),
        ("Update ALU\n", None),
        ("fix(core): clean\n\nfirst\nsecond\n", "Body is 2 line(s)"),
        ("fix(core): codex mention\n", "Banned phrase"),
        ("Merge branch 'feature'\n", None),
        ("fix(core): clean\r\n\r\nbody\r\n", None),
    ],
)
def test_validation_uses_real_project_policy(
    tmp_path: Path,
    message: str,
    error_fragment: str | None,
) -> None:
    root = _configured_project(
        tmp_path,
        "enabled = true\nmax_body_lines = 1\n",
    )

    errors = validate_message(message, project_root=root)

    if error_fragment is None:
        assert errors == []
    else:
        assert any(error_fragment in error for error in errors)


def test_source_checkout_exemption_overrides_config() -> None:
    source_root = Path(__file__).resolve().parents[2]

    assert policy.stealth_policy(source_root) == StealthPolicy(False, (), None, ())


def test_default_validation_preserves_ordinary_engineering_prose(tmp_path: Path) -> None:
    message = "fix(core): generated docker verification agent\n\nauto-review automated fix suggested fix\n"
    assert validate_message(message, project_root=tmp_path) == []
    _configured_project(tmp_path, 'enabled = true\nbanned_words = ["generated"]\n')
    assert any(
        "Banned phrase" in error for error in validate_message(message, project_root=tmp_path)
    )


def test_identifier_message_validation_uses_both_tiers(tmp_path):
    root = _configured_project(
        tmp_path,
        'enabled = true\nbanned_words = ["booley", "agent"]\nbanned_substrings = ["quokka"]\n',
    )
    assert validate_message("fix: booley_config BooleyRunner myquokkafile\n", project_root=root)
    assert validate_message("fix: reagent precursor\n", project_root=root) == []


def test_bundle_import_falls_back_to_flat_commit_policy_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inside the hook bundle the installed package is absent; the flat module is used."""
    import importlib.util
    import sys
    import types

    from booley.commit_policy import validation

    flat = types.ModuleType("booley_commit_policy")
    flat.__dict__.update(
        find_banned=lambda text, project_root=None: [],
        max_body_lines=lambda project_root=None: 1,
        stealth_enabled=lambda project_root=None: True,
    )
    monkeypatch.setitem(sys.modules, "booley.commit_policy.policy", None)
    monkeypatch.setitem(sys.modules, "booley_commit_policy", flat)
    spec = importlib.util.spec_from_file_location("bundled_validation", validation.__file__)
    assert spec is not None and spec.loader is not None
    bundled = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundled)

    # The flat module's cap of one body line is what the bundled copy enforces.
    assert bundled.body_length_errors("Update ALU\n\none\ntwo\n")
    assert bundled.body_length_errors("Update ALU\n\none\n") == []
