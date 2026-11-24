"""Tests for shared commit policy and Project configuration loading."""

from __future__ import annotations

import builtins
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from booley.commit_policy.policy import (
    StealthPolicy,
    allowed_authors,
    banned_phrases,
    find_banned,
    has_banned_content,
    identity_allowed,
    max_body_lines,
    parse_stealth_vocabulary,
    source_checkout_policy_owner,
    stealth_choice_unset,
    stealth_enabled,
    stealth_policy,
    validate_push_configuration,
)
from booley.commit_policy.validation import validate_message
from booley.core.boundary import BoundaryError

SOURCE_CHECKOUT = "[tool.booley]\nsource_checkout = true\n"


def write_project(root: Path, toml_body: bytes | None) -> Path:
    """Create ``root/.booley`` and, unless *toml_body* is None, its ``booley.toml``."""
    directory = root / ".booley"
    directory.mkdir(parents=True)
    if toml_body is not None:
        (directory / "booley.toml").write_bytes(toml_body)
    return root


# ---------------------------------------------------------------------------
# find_banned / has_banned_content (use the default banned list)
# ---------------------------------------------------------------------------

DEFAULT_PHRASES = [
    "claude",
    "anthropic",
    "copilot",
    "codex",
    "openai",
    "chatgpt",
    "gemini",
    "booley",
    "gpt",
    "llm",
    "co-authored-by",
]


@pytest.mark.parametrize("phrase", DEFAULT_PHRASES)
def test_default_inventory_reports_each_phrase(phrase: str) -> None:
    """Each default phrase is banned, whatever its case."""
    assert phrase in find_banned(f"written with {phrase.upper()} today")
    assert has_banned_content(phrase) is True


@pytest.mark.parametrize(
    "text",
    [
        "fix a memory leak in the allocator",
        "normal commit message",
        "",
        # Ordinary engineering vocabulary is not banned by default.
        "generated agent docker auto-review auto-approve per review findings "
        "as suggested by automated fix suggested fix",
        "ticket_fifo cursor_reg",
        "precursor.txt reagent.sv",
    ],
)
def test_ordinary_text_is_clean(text: str) -> None:
    assert find_banned(text) == []
    assert has_banned_content(text) is False


def test_find_banned_escapes_regex_metacharacters_and_respects_boundaries() -> None:
    """A hyphen in a phrase is literal and an empty vocabulary bans nothing."""
    policy = StealthPolicy(True, ("auto-review", "agent"), None, ())
    assert policy.find_banned("this is auto-review stuff") == ["auto-review"]
    assert policy.find_banned("autoreview reagent") == []
    assert StealthPolicy(True, (), None, ()).find_banned("claude") == []


# ---------------------------------------------------------------------------
# [stealth] settings read from the Project configuration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("toml_body", "expected"),
    [
        pytest.param(None, False, id="no-config"),
        pytest.param(b"[jobs]\nmax_tickets = 2\n", False, id="no-stealth-section"),
        pytest.param(b'[stealth]\nbanned_words = ["foo"]\n', False, id="enabled-omitted"),
        pytest.param(b"[stealth\nenabled = false\n", True, id="unparsable-fails-closed"),
        pytest.param(b"[stealth]\nenabled = false\n", False, id="disabled"),
        pytest.param(b"[stealth]\nenabled = true\n", True, id="enabled"),
        pytest.param(b'[stealth]\nenabled = false\nbanned_words = ["foo"]\n', False, id="words"),
        pytest.param(b'stealth = "yes"\n', False, id="malformed-section-ignored"),
    ],
)
def test_stealth_enabled_reads_the_project_config(
    tmp_path: Path, toml_body: bytes | None, expected: bool
) -> None:
    assert stealth_enabled(write_project(tmp_path, toml_body)) is expected


def test_banned_words_coexist_with_the_enabled_choice(tmp_path: Path) -> None:
    root = write_project(tmp_path, b'[stealth]\nenabled = false\nbanned_words = ["foo"]\n')
    policy = stealth_policy(root)
    assert policy.enabled is False
    assert policy.banned_phrases == ("foo",)
    assert stealth_choice_unset(root) is False


def test_stealth_choice_is_unset_without_the_enabled_key(tmp_path: Path) -> None:
    assert stealth_choice_unset(write_project(tmp_path, b'[stealth]\nbanned_words = ["a"]\n'))


def test_source_checkout_is_never_stealth_even_with_stale_config(tmp_path: Path) -> None:
    root = write_project(tmp_path, b"[stealth]\nenabled = true\n")
    (root / "pyproject.toml").write_text(SOURCE_CHECKOUT, encoding="utf-8")

    assert stealth_enabled(root) is False
    assert banned_phrases(root) == []
    assert stealth_choice_unset(root) is False


def test_invalid_enabled_type_logs_and_uses_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    root = write_project(tmp_path, b'[stealth]\nenabled = "true"\n')

    assert stealth_enabled(root) is False
    assert "[stealth] enabled must be a boolean" in caplog.text


@pytest.mark.parametrize(
    ("toml_body", "expected"),
    [
        pytest.param(None, None, id="no-config"),
        pytest.param(b'[stealth]\nbanned_words = ["foo"]\n', None, id="knob-absent"),
        pytest.param(b"[stealth]\nmax_body_lines = 0\n", 0, id="zero-is-a-real-cap"),
        pytest.param(b"[stealth]\nmax_body_lines = 5\n", 5, id="positive"),
        pytest.param(b"[stealth]\nmax_body_lines = -1\n", None, id="negative-ignored"),
        pytest.param(b"[stealth]\nmax_body_lines = true\n", None, id="bool-is-not-an-int"),
        pytest.param(b'[stealth]\nmax_body_lines = "many"\n', None, id="non-int-ignored"),
    ],
)
def test_max_body_lines_reads_an_optional_cap(
    tmp_path: Path, toml_body: bytes | None, expected: int | None
) -> None:
    assert max_body_lines(write_project(tmp_path, toml_body)) == expected


def test_retired_convention_key_is_ignored(tmp_path: Path) -> None:
    """The retired subject-convention key is ignored, never an error."""
    write_project(
        tmp_path,
        b"[stealth]\nenabled = true\nenforce_convention = true\nmax_body_lines = 2\n",
    )

    policy = stealth_policy(tmp_path)

    assert policy.enabled is True
    assert policy.max_body_lines == 2
    assert validate_message("Update ALU\n", project_root=tmp_path) == []


@pytest.mark.parametrize(
    ("toml_body", "expected"),
    [
        pytest.param(None, [], id="no-config"),
        pytest.param(
            b'[stealth]\nallowed_authors = ["*@example.com", "Jane Doe"]\n',
            ["*@example.com", "Jane Doe"],
            id="list",
        ),
        pytest.param(b'[stealth]\nallowed_authors = ["a@b.c", "  ", ""]\n', ["a@b.c"], id="blank"),
        pytest.param(b'[stealth]\nallowed_authors = "a@b.c"\n', [], id="non-list"),
        pytest.param(b"[stealth]\nallowed_authors = [1, 2]\n", [], id="non-string"),
    ],
)
def test_allowed_authors_reads_the_identity_allowlist(
    tmp_path: Path, toml_body: bytes | None, expected: list[str]
) -> None:
    assert allowed_authors(write_project(tmp_path, toml_body)) == expected


@pytest.mark.parametrize(
    ("name", "email", "allow", "expected"),
    [
        pytest.param("Anyone", "any@where", [], True, id="empty-allows-everything"),
        pytest.param("Jane Doe", "jane@example.com", ["jane@example.com"], True, id="email"),
        pytest.param("Jane Doe", "jane@other.com", ["jane@example.com"], False, id="other-email"),
        pytest.param("Jane Doe", "jane@example.com", ["*@example.com"], True, id="domain-glob"),
        pytest.param("Bot", "mut@local", ["*@example.com"], False, id="outside-domain"),
        pytest.param("Jane Doe", "whatever@anywhere", ["Jane Doe"], True, id="bare-name"),
        pytest.param(
            "Jane Doe", "jane@example.com", ["Jane Doe <jane@example.com>"], True, id="full-ident"
        ),
        pytest.param("JANE DOE", "JANE@EXAMPLE.COM", ["jane@example.com"], True, id="upper"),
        pytest.param("jane doe", "jane@example.com", ["JANE DOE"], True, id="lower"),
        pytest.param("  Jane Doe  ", " jane@example.com ", ["jane@example.com"], True, id="space"),
        pytest.param(
            "mut-creator",
            "mut@local",
            ["*@example.com", "*@users.noreply.github.com"],
            False,
            id="fabricated-author",
        ),
    ],
)
def test_identity_allowed_globs_email_name_and_full_ident(
    name: str, email: str, allow: list[str], expected: bool
) -> None:
    assert identity_allowed(name, email, allow) is expected


@pytest.mark.parametrize(
    ("data_name", "enabled", "source", "expected"),
    [
        pytest.param("booley.toml", True, False, True, id="booley-on"),
        pytest.param("booley.toml", False, False, False, id="booley-off"),
        pytest.param("pipeline.toml", True, False, True, id="pipeline-on"),
        pytest.param("pipeline.toml", False, False, False, id="pipeline-off"),
        pytest.param("booley.toml", True, True, False, id="source-checkout-stays-off"),
    ],
)
def test_stealth_uses_explicit_relocated_project_data(
    tmp_path: Path, data_name: str, enabled: bool, source: bool, expected: bool
) -> None:
    root, data = tmp_path / "checkout", tmp_path / "relocated"
    root.mkdir()
    data.mkdir()
    if source:
        (root / "pyproject.toml").write_text(SOURCE_CHECKOUT, encoding="utf-8")
    (data / data_name).write_text(f"[stealth]\nenabled = {str(enabled).lower()}\n", "utf-8")
    assert stealth_enabled(root, booley_dir=data) is expected


# ---------------------------------------------------------------------------
# source_checkout_policy_owner import fallbacks
# ---------------------------------------------------------------------------


def test_source_tree_fallback_adds_package_parent() -> None:
    """A directly launched source hook can recover the canonical classifier."""
    source_root = Path(__file__).resolve().parents[2]
    real_import = builtins.__import__
    package_attempts = 0

    def import_after_path_recovery(name, *args, **kwargs):
        nonlocal package_attempts
        if name == "booley.core.checkout_role":
            package_attempts += 1
            if package_attempts == 1:
                raise ModuleNotFoundError(name)
        if name == "checkout_role":
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    path_without_source = [
        entry for entry in sys.path if Path(entry or ".").resolve() != source_root.resolve()
    ]
    with (
        patch.object(sys, "path", path_without_source),
        patch("builtins.__import__", side_effect=import_after_path_recovery),
    ):
        assert source_checkout_policy_owner(source_root)

    assert package_attempts == 2


def test_source_tree_fallback_is_fail_closed_when_classifier_cannot_load() -> None:
    """An existing classifier that cannot import never guesses source ownership."""
    source_root = Path(__file__).resolve().parents[2]
    real_import = builtins.__import__

    def block_classifiers(name, *args, **kwargs):
        if name in {"booley.core.checkout_role", "checkout_role"}:
            raise ModuleNotFoundError(name)
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=block_classifiers):
        assert not source_checkout_policy_owner(source_root)


# ---------------------------------------------------------------------------
# validate_push_configuration
# ---------------------------------------------------------------------------


def _push_config(root: Path) -> Path:
    """Return the Project's ``booley.toml`` path, creating its directory."""
    config = root / ".booley" / "booley.toml"
    config.parent.mkdir()
    return config


@pytest.mark.parametrize(
    "text",
    [
        'upstream_repository = "/upstream"',
        'upstream_base = "' + "f" * 40 + '"',
        'upstream_repository = ""\nupstream_base = "' + "f" * 40 + '"',
        'upstream_repository = 3\nupstream_base = "' + "f" * 40 + '"',
        'upstream_repository = "/upstream"\nupstream_base = "main"',
        'upstream_repository = "/upstream"\nupstream_base = "' + "f" * 39 + '"',
        'upstream_repository = "/upstream"\nupstream_base = 42',
        'upstream_repository = "/upstream"\nupstream_base = "' + "0" * 40 + '"',
        "upstream_repository = [",
    ],
)
def test_validate_push_configuration_invalid_pair_fails_closed(tmp_path: Path, text: str) -> None:
    _push_config(tmp_path).write_text("[stealth]\n" + text)
    with pytest.raises(ValueError):
        validate_push_configuration(tmp_path)


def test_validate_push_configuration_absent_and_immutable_pairs(tmp_path: Path) -> None:
    assert validate_push_configuration(tmp_path) is None
    config = _push_config(tmp_path)
    # Transport validation is left to the hook, so any repository string passes through.
    for repository, base in (
        ("https://example.test/upstream.git", "A" * 40),
        ("ext::x", "B" * 64),
    ):
        config.write_text(
            f'[stealth]\nupstream_repository = "{repository}"\nupstream_base = "{base}"\n'
        )
        upstream = validate_push_configuration(tmp_path)
        assert upstream is not None
        assert (upstream.repository, upstream.base) == (repository, base.lower())


@pytest.mark.parametrize(
    ("denied_method", "config_exists"),
    [
        pytest.param("open", True, id="unreadable-config"),
        pytest.param("stat", False, id="stat-error"),
        # stat() says missing but lstat() cannot tell: refuse rather than assume absent.
        pytest.param("lstat", False, id="unreadable-missing-node"),
    ],
)
def test_validate_push_configuration_filesystem_error_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, denied_method: str, config_exists: bool
) -> None:
    config = _push_config(tmp_path)
    if config_exists:
        config.write_text("[stealth]\n")
    original = getattr(Path, denied_method)

    def denied(path, *args, **kwargs):
        if path == config:
            raise PermissionError("denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, denied_method, denied)
    with pytest.raises(ValueError, match="cannot read selected") as caught:
        validate_push_configuration(tmp_path)
    assert isinstance(caught.value.__cause__, PermissionError)


def test_validate_push_configuration_source_owner_never_loads_project_config(
    tmp_path: Path,
) -> None:
    """A source checkout skips even a Project configuration that could not be parsed."""
    (tmp_path / "pyproject.toml").write_text(SOURCE_CHECKOUT, encoding="utf-8")
    _push_config(tmp_path).write_bytes(b"[stealth\n")
    assert validate_push_configuration(tmp_path) is None


@pytest.mark.parametrize("target", ["missing.toml", "booley.toml"])
def test_validate_push_configuration_broken_config_node_refuses(
    tmp_path: Path, target: str
) -> None:
    try:
        _push_config(tmp_path).symlink_to(target)
    except OSError:
        pytest.skip("platform does not support symlink creation")
    with pytest.raises(ValueError, match="cannot read selected"):
        validate_push_configuration(tmp_path)


def test_validate_push_configuration_non_table_stealth_refuses(tmp_path: Path) -> None:
    _push_config(tmp_path).write_text('stealth = "enabled"\n')
    with pytest.raises(ValueError, match=r"\[stealth\] must be a table"):
        validate_push_configuration(tmp_path)


# ---------------------------------------------------------------------------
# Vocabulary tiers and identifier matching
# ---------------------------------------------------------------------------


def test_identifier_reported_spellings_and_generic_controls() -> None:
    spellings = (
        "docs_qa/booley_notes.txt",
        "notes/booley-notes.txt",
        "notes/mybooleyfile.txt",
        "booley_config",
        "BooleyRunner",
    )
    assert [find_banned(text) for text in spellings] == [["booley"]] * 5
    custom = StealthPolicy(True, ("agent",), None, ())
    assert "agent" in custom.find_banned("axi_agent XMLAgent reAgent Agent4")
    assert "gpt" in find_banned("4gpt7 GPT4")


def test_vocabulary_config_union_reload_and_empty(tmp_path: Path) -> None:
    write_project(tmp_path, None)
    config = tmp_path / ".booley" / "booley.toml"
    config.write_text(
        '[stealth]\nbanned_words = ["Quokka", "booley", "QUOKKA"]\nbanned_substrings = ["quokka", "Narwhal"]\n'
    )
    policy = stealth_policy(tmp_path)
    assert policy.banned_phrases == ("Quokka", "booley", "Narwhal")
    assert policy.banned_substrings == ("booley", "quokka", "Narwhal")
    assert policy.find_banned("myquokkafile myBooleyfile NarwhalRunner") == [
        "Quokka",
        "booley",
        "Narwhal",
    ]
    config.write_text("[stealth]\nbanned_words = []\n")
    assert stealth_policy(tmp_path).banned_phrases == ()
    config.write_text('[stealth]\nbanned_words = []\nbanned_substrings = ["quokka"]\n')
    assert stealth_policy(tmp_path).find_banned("myquokkafile") == ["quokka"]


@pytest.mark.parametrize(
    "value",
    [
        "quokka",
        [1],
        None,
        ["red"],
        ["act"],
        ["mod"],
        ["auth"],
        ["<module-42>"],
        ["42>"],
        ["<"],
        ["ule-"],
        ["\u0131"],
        [">/<"],
    ],
)
def test_substring_tier_rejects_malformed_and_reserved(value: object) -> None:
    with pytest.raises(BoundaryError, match="banned_substrings"):
        parse_stealth_vocabulary({"banned_substrings": value})


def test_reserved_substring_is_named_with_the_reason() -> None:
    """A reserved entry is named and the reason (feedback redaction) is given."""
    with pytest.raises(BoundaryError, match="feedback-redaction placeholder") as reserved:
        parse_stealth_vocabulary({"banned_substrings": ["quokka", "<module-42>"]})
    assert "'<module-42>'" in str(reserved.value)


def test_matching_contract() -> None:
    policy = StealthPolicy(
        True,
        ("agent", "cursor", "per review findings", "booley", "anthropic", "claude"),
        None,
        (),
    )
    for text in ("axi_agent", "XMLAgent", "reAgent", "Agent4", "éagentΩ"):
        assert "agent" in policy.find_banned(text)
    assert policy.find_banned("reagent precursor per-review-findings per  review findings") == []
    assert policy.find_banned("per review findings") == ["per review findings"]
    assert policy.find_banned("philanthropic Claudette myBOOLEYfile") == [
        "booley",
        "anthropic",
        "claude",
    ]
    assert "cursor" in policy.find_banned("preCursor")


def test_cache_key_separates_tiers() -> None:
    token = StealthPolicy(True, ("quokka",), None, ())
    substring = StealthPolicy(True, ("quokka",), None, (), ("quokka",))
    assert token.find_banned("myquokkafile") == []
    assert substring.find_banned("myquokkafile") == ["quokka"]


def test_source_disabled_ignores_new_tier(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(SOURCE_CHECKOUT)
    write_project(tmp_path, b'[stealth]\nbanned_substrings = ["red"]\n')
    assert stealth_policy(tmp_path) == StealthPolicy(False, (), None, ())


def test_token_ascii_edges_and_overlapping_repeated_literal(tmp_path: Path) -> None:
    assert StealthPolicy(True, ("agent",), None, ()).find_banned("\u212aagent\u0131") == ["agent"]
    write_project(tmp_path, b'[stealth]\nbanned_words = []\nbanned_substrings = ["aba"]\n')
    assert find_banned("é ababa Ω", tmp_path) == ["aba"]


def test_all_identity_overrides_match_the_loaded_policy() -> None:
    identities = (
        "booley",
        "claude",
        "anthropic",
        "codex",
        "openai",
        "chatgpt",
        "copilot",
        "gemini",
    )
    union, substrings = parse_stealth_vocabulary({"banned_words": list(identities)})
    direct = StealthPolicy(True, identities, None, ())
    loaded = StealthPolicy(True, union, None, (), substrings)
    for term in identities:
        text = "my" + term.upper() + "file"
        assert direct.find_banned(text) == loaded.find_banned(text) == [term]


def test_disabled_policy_rejects_invalid_explicit_substring_tier(tmp_path: Path) -> None:
    write_project(tmp_path, b'[stealth]\nenabled = false\nbanned_substrings = ["red"]\n')
    with pytest.raises(BoundaryError, match="reserved"):
        stealth_policy(tmp_path)
