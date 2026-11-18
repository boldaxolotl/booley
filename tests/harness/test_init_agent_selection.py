"""Agent selection and defaulting contract for ``booley init`` and ``--seed``."""

from __future__ import annotations

import argparse
import builtins
import sys
import warnings
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from booley.harness import init_cmd
from booley.harness.setup.common import InitContext
from booley.projects import inventory
from booley.runtime import session_runtime as sr
from booley.runtime.image_lifecycle import Diagnostic, ImageCleanup
from tests.conftest import symlink_or_skip
from tests.docker_cli_guard import docker_cli_absent
from tests.harness.init_agent_support import (
    api_key,
    empty_host,
    login,
    project_config,
    seed_init,
    stub_host_images,
)
from tests.subprocess_budget import run_bounded_child

_SANDBOX_IMAGE = "sha256:" + "f" * 64
_CURRENT = init_cmd.LifecycleResult(
    "booley-sandbox-riscv", _SANDBOX_IMAGE, init_cmd.ImageLifecycleStatus.CURRENT
)


@pytest.fixture(autouse=True)
def _empty_host(tmp_path, monkeypatch):
    """Detection reads the host's credentials: run every test on an empty one."""
    return empty_host(tmp_path, monkeypatch)


def test_initial_line_ending_baseline_does_not_warn_for_uncreated_data(tmp_path):
    ctx = InitContext(project_root=tmp_path)
    with init_cmd.init_booley_dir_scope(tmp_path), warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # private: the baseline is sampled inside host and Sandbox init before any write,
        # and the warning it must not raise does not reach init's records.
        assert init_cmd._sample_line_ending_baseline(ctx) == {}
    assert caught == []
    assert not init_cmd.booley_dir_for_init(tmp_path).exists()


def test_seed_retains_ancestor_project_resolution(tmp_path, monkeypatch):
    parent_config = project_config(tmp_path, b'[agent]\nprovider = "codex"\n')
    child = tmp_path / "ticket-checkout"
    child.mkdir()
    stub_host_images(monkeypatch)

    seed_init(child)

    assert parent_config.read_bytes() == b'[agent]\nprovider = "codex"\nauth = "subscription"\n'
    assert not (child / ".booley").exists()


@pytest.mark.parametrize("relocated_parent", [False, True])
@pytest.mark.parametrize("child_guidance", [False, True])
def test_nested_child_run_init_isolates_every_project_write(
    tmp_path, monkeypatch, relocated_parent, child_guidance
):
    parent_booley_dir = tmp_path / ("project-data" if relocated_parent else ".booley")
    parent_config = parent_booley_dir / "booley.toml"
    parent_config.parent.mkdir()
    original = '[agent]\nprovider = "claude"\nauth = "auto"\n\n[interactive]\nidle_timeout_seconds = 600\n'
    parent_config.write_text(original, encoding="utf-8")
    parent_guidance = parent_booley_dir / "AGENTS.md"
    parent_guidance.write_bytes(b"# Parent Project\r\n")
    if relocated_parent:
        (tmp_path / "booley.toml").write_bytes(b'[project]\ndir = "project-data"\n')
    child = tmp_path / "child"
    child.mkdir()
    if child_guidance:
        (child / "AGENTS.md").write_bytes(b"# Child Project\n")
    parent_git_config = _initialize_nested_git_repositories(parent_booley_dir, child)
    parent_entries = set(parent_booley_dir.iterdir())
    monkeypatch.setenv("BOOLEY_DIR", str(parent_config.parent))
    init_cmd.reset_cache()
    assert init_cmd.resolve_booley_dir() == parent_config.parent.resolve()

    sandbox_calls, docker_calls = _stub_nested_init_services(tmp_path, monkeypatch)

    args = argparse.Namespace(
        seed=False, provider="codex", auth="subscription", skip_credentials=True
    )
    assert init_cmd.run_init(args, child) == 0

    child_config = child / ".booley" / "booley.toml"
    content = child_config.read_text(encoding="utf-8")
    assert 'default = "codex"' in content
    assert 'auth = "subscription"' in content
    assert not (child_config.parent / "tickets").exists()
    assert parent_config.read_text(encoding="utf-8") == original
    assert set(parent_booley_dir.iterdir()) == parent_entries
    assert (parent_booley_dir / ".git" / "config").read_bytes() == parent_git_config
    assert parent_guidance.read_bytes() == b"# Parent Project\r\n"
    assert not (child / "CLAUDE.md").exists()
    if child_guidance:
        assert (child / "AGENTS.md").read_bytes() == b"# Child Project\n"
    else:
        assert not (child / "AGENTS.md").exists()
    assert init_cmd.resolve_booley_dir() == parent_booley_dir.resolve()
    assert docker_calls == [["run", "--rm", _SANDBOX_IMAGE, "git", "--version"]]
    assert sandbox_calls == [
        {
            "nangate_pdk_root": tmp_path / "pdk",
            "agent_app": "codex",
            "runtime_image_id": _SANDBOX_IMAGE,
            "init_will_create_booley_dir": True,
        }
    ]
    assert str(child.resolve()) in inventory.known_roots()


def _stub_nested_init_services(tmp_path, monkeypatch):
    """Stand in for the Docker-backed services; return the Sandbox and Docker calls made."""
    stub_host_images(monkeypatch, _CURRENT)
    sandbox_calls = []
    # private: Sandbox Issuance needs Docker.
    monkeypatch.setattr(
        init_cmd, "_step_sandbox", lambda _ctx, **kwargs: sandbox_calls.append(kwargs)
    )
    monkeypatch.setattr(init_cmd.nangate_pdk, "cache_root", lambda: tmp_path / "pdk")

    from booley.runtime import issuance_invalidation, lifecycle_lock

    monkeypatch.setattr(lifecycle_lock, "host_lifecycle_lock", lambda _purpose: nullcontext())
    monkeypatch.setattr(
        issuance_invalidation,
        "shared_recovery_blocks_command",
        lambda *, read_only: False,
    )
    return sandbox_calls, docker_cli_absent(monkeypatch)


def _initialize_nested_git_repositories(parent_booley_dir, child):
    """Use real Git so an escaped repair would alter the parent's policy or files."""
    commands = (
        (parent_booley_dir, "init", "-q"),
        (child, "init", "-q"),
        (parent_booley_dir, "config", "core.autocrlf", "true"),
        (parent_booley_dir, "add", "-A"),
        (
            parent_booley_dir,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "commit",
            "-qm",
            "parent project",
        ),
    )
    for root, *args in commands:
        run_bounded_child(["git", "-C", str(root), *args], check=True)
    return (parent_booley_dir / ".git" / "config").read_bytes()


_AGENT_GIT = b'[agent]\nprovider = "claude"\n\n[agent.git]\nname = "Expected Developer"\n'
_TABLE_COMMENTS = (
    b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n\n'
    b'[agent.auth]\n# billing\nclaude = "api_key"  # keep\ncodex = "auto"\n\n[flows]\nx = 1\n'
)


@pytest.mark.parametrize(
    ("before", "flags", "after"),
    [
        pytest.param(
            None, {}, b'[agent]\ndefault = "claude"\nauth = "subscription"\n', id="missing-file"
        ),
        pytest.param(
            b"# c\n[flows.sim]\nenabled = false\n",
            {},
            b"# c\n[flows.sim]\nenabled = false\n"
            b'\n[agent]\ndefault = "claude"\nauth = "subscription"\n',
            id="no-agent-table",
        ),
        pytest.param(
            b'[agent]\nprovider = "claude"\n',
            {},
            b'[agent]\nprovider = "claude"\nauth = "subscription"\n',
            id="provider-only",
        ),
        pytest.param(
            b'[agent]\nauth = "subscription"\n',
            {},
            b'[agent]\nauth = "subscription"\ndefault = "claude"\n',
            id="auth-without-default",
        ),
        pytest.param(
            b'[agent]\nprovider = "codex"\n\n[sandbox]\nenabled = true\n',
            {},
            b'[agent]\nprovider = "codex"\nauth = "subscription"\n\n[sandbox]\nenabled = true\n',
            id="trailing-blank",
        ),
        pytest.param(
            b'[agent]\nprovider = "codex"\n[sandbox]\nenabled = true\n',
            {},
            b'[agent]\nprovider = "codex"\nauth = "subscription"\n[sandbox]\nenabled = true\n',
            id="no-blank",
        ),
        pytest.param(
            b'[sandbox]\nenabled = true\n\n[agent]\nprovider = "codex"\n\n',
            {},
            b'[sandbox]\nenabled = true\n\n[agent]\nprovider = "codex"\nauth = "subscription"\n\n',
            id="last-table",
        ),
        pytest.param(
            b'[agent]\nprovider = "codex"\nnotes = [\n  ["a", "b"],\n]\n\n[sandbox]\n',
            {},
            b'[agent]\nprovider = "codex"\nnotes = [\n  ["a", "b"],\n]\n'
            b'auth = "subscription"\n\n[sandbox]\n',
            id="nested-array",
        ),
        pytest.param(
            b'[agent]\nprovider = "codex"\n\n# sandbox settings\n[sandbox]\nenabled = true\n',
            {},
            b'[agent]\nprovider = "codex"\n\n# sandbox settings\nauth = "subscription"\n'
            b"[sandbox]\nenabled = true\n",
            id="comment-before-header",
        ),
        pytest.param(
            b'[agent.git]\nname = "E"\n',
            {},
            b'[agent]\ndefault = "claude"\nauth = "subscription"\n\n[agent.git]\nname = "E"\n',
            id="agent-git-only",
        ),
        pytest.param(
            _AGENT_GIT,
            {},
            _AGENT_GIT.replace(b'"claude"\n\n', b'"claude"\nauth = "subscription"\n\n', 1),
            id="agent-git-identity",
        ),
        pytest.param(
            _TABLE_COMMENTS,
            {},
            _TABLE_COMMENTS.replace(b'codex = "auto"', b'codex = "subscription"'),
            id="auth-table-comments",
        ),
        pytest.param(
            b'[agent]\nauth = "auto"  # billing\ndefault = "claude"\n',
            {},
            b'[agent]\nauth = "subscription"  # billing\ndefault = "claude"\n',
            id="auto-string-comment",
        ),
        pytest.param(
            b'[agent]\ndefault = "claude"\n',
            {"auth": "api_key"},
            b'[agent]\ndefault = "claude"\nauth = "api_key"\n',
            id="auth-flag",
        ),
    ],
)
def test_init_records_missing_agent_keys(tmp_path, monkeypatch, before, flags, after):
    """Init adds only the missing ``[agent]`` keys and leaves every other byte alone.

    New keys land after the table's last non-blank line (40e468e49); the current key
    ``default`` is written, never the older ``provider`` (#3227); the file keeps LF
    line endings (#609); a retired ``auto`` is replaced in place, keeping its comment.
    """
    config = project_config(tmp_path, before)
    stub_host_images(monkeypatch)

    seed_init(tmp_path, **flags)

    assert config.read_bytes() == after


@pytest.mark.parametrize(
    ("declared", "flags", "message"),
    [
        pytest.param(
            b'[agent]\ndefault = "codex"\n',
            {"provider": "claude"},
            "--provider=claude conflicts with booley.toml [agent] default='codex'",
            id="default",
        ),
        pytest.param(
            b'[agent]\nprovider = "codex"\n',
            {"provider": "claude"},
            "--provider=claude conflicts with booley.toml [agent] default='codex'",
            id="provider-alias",
        ),
        pytest.param(
            b'[agent]\nclis = ["codex"]\n',
            {"provider": "claude"},
            "--provider=claude conflicts with booley.toml [agent] default='codex'",
            id="clis",
        ),
        pytest.param(
            b'[agent]\nclis = ["claude", "codex"]\ndefault = "claude"\n\n'
            b'[agent.auth]\nclaude = "api_key"\ncodex = "auto"\n',
            {"auth": "subscription"},
            "--auth=subscription conflicts with booley.toml [agent] auth=",
            id="auth-table",
        ),
    ],
)
def test_conflicting_flag_is_refused(tmp_path, monkeypatch, capsys, declared, flags, message):
    """The conflict error names the config key `[agent] default`, whichever spelling declared it."""
    config = project_config(tmp_path, declared)
    reached = stub_host_images(monkeypatch)

    seed_init(tmp_path, **flags)

    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert message in output
    assert "[agent] provider" not in output
    assert config.read_bytes() == declared
    assert reached == []


@pytest.mark.parametrize("check_only", [False, True])
def test_agent_config_refuses_symlinked_booley_toml(tmp_path, monkeypatch, capsys, check_only):
    destination = tmp_path / "outside.toml"
    destination.write_bytes(b"# elsewhere\n")
    config = project_config(tmp_path)
    symlink_or_skip(config, destination)
    planted_target = config.readlink()
    stub_host_images(monkeypatch)

    seed_init(tmp_path, check_only=check_only)

    assert destination.read_bytes() == b"# elsewhere\n"
    assert config.readlink() == planted_target
    captured = capsys.readouterr()
    assert "refusing symlinked" in captured.out + captured.err


@pytest.mark.parametrize("check_only", [False, True], ids=["write", "check-only"])
def test_seed_hands_the_resolved_provider_to_the_sandbox_step(
    tmp_path, monkeypatch, capsys, check_only
):
    from booley.presentation import colors

    monkeypatch.setattr(colors, "COLORS_ENABLED", False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    stub_host_images(monkeypatch, _CURRENT)
    if not check_only:
        project_config(tmp_path)
    handed = []

    def sandbox_step(_ctx, **kwargs):
        config = tmp_path / ".booley" / "booley.toml"
        handed.append((kwargs["agent_app"], config.read_bytes() if config.exists() else None))

    # private: Sandbox Issuance needs Docker.
    monkeypatch.setattr(init_cmd, "_step_sandbox", sandbox_step)

    seed_init(tmp_path, provider="codex", check_only=check_only)

    out = capsys.readouterr().out
    if check_only:
        assert handed == [("codex", None)]
        assert not (tmp_path / ".booley").exists()
        assert "would record [agent] codex/subscription" in out
    else:
        assert handed == [("codex", b'[agent]\ndefault = "codex"\nauth = "subscription"\n')]
        assert f"Seeding the Sandbox for {tmp_path}\n" in out


@pytest.mark.parametrize("inspection", ["fails", "passes"])
def test_seed_stops_when_existing_guidance_cannot_be_inspected(
    tmp_path, monkeypatch, capsys, inspection
):
    booley_dir = project_config(tmp_path, b'[agent]\ndefault = "claude"\n').parent
    (booley_dir / "AGENTS.md").write_bytes(b"# Project\n")
    reached = stub_host_images(monkeypatch)
    if inspection == "fails":

        def unreadable(*_args):
            raise OSError("unreadable")

        # No portable file state makes the guidance inspection raise.
        monkeypatch.setattr(init_cmd, "plan_guidance_links", unreadable)

    seed_init(tmp_path)

    config = (booley_dir / "booley.toml").read_bytes()
    if inspection == "fails":
        assert "initialization filesystem inspection failed: unreadable" in capsys.readouterr().out
        assert config == b'[agent]\ndefault = "claude"\n'
        assert reached == []
    else:
        assert config == b'[agent]\ndefault = "claude"\nauth = "subscription"\n'
        assert reached == [tmp_path]


def _select_at_terminal(root, **flags):
    """Resolve and persist the selection with a terminal; return the last record."""
    ctx = InitContext(project_root=root, interactive=True, show_step_banners=False)
    config = project_config(root)
    # private: `run_init` takes `interactive` from `sys.stdin.isatty`, bound when booley is
    # imported, so a test cannot give it a terminal; prompts are driven at the selection step.
    selection = init_cmd._resolve_agent_selection(ctx, argparse.Namespace(**flags), config)
    if selection is not None:
        init_cmd._step_agent_config(ctx, selection, config)  # private: same reason
    return ctx.results[-1]


def _answer_prompts(monkeypatch, *answers):
    """Answer each prompt in turn, then end the input; return the prompts asked."""
    prompts = []
    remaining = iter(answers)

    def answer(prompt):
        prompts.append(prompt)
        try:
            return next(remaining)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", answer)
    return prompts


_DEFAULT_CLAUDE = b'[agent]\ndefault = "claude"\nauth = "%s"\n'


@pytest.mark.parametrize(
    ("credentials", "answers", "prompts", "written"),
    [
        pytest.param((), ("",), 1, _DEFAULT_CLAUDE % b"subscription", id="provider-default"),
        pytest.param((), ("other", "still-other", "nope"), 3, None, id="three-invalid-answers"),
        pytest.param(
            ("login", "key"), ("", ""), 2, _DEFAULT_CLAUDE % b"subscription", id="auth-default"
        ),
        pytest.param(
            ("login", "key"), ("", "api-key"), 2, _DEFAULT_CLAUDE % b"api_key", id="auth-api-key"
        ),
        pytest.param(("login", "key"), ("",), 2, None, id="auth-aborted"),
        pytest.param(("key",), ("",), 1, _DEFAULT_CLAUDE % b"api_key", id="single-credential"),
    ],
)
def test_terminal_prompts_resolve_or_abort(
    tmp_path, monkeypatch, capsys, _empty_host, credentials, answers, prompts, written
):
    if "login" in credentials:
        login(_empty_host, "claude")
    if "key" in credentials:
        api_key(monkeypatch, "claude")
    asked = _answer_prompts(monkeypatch, *answers)

    result = _select_at_terminal(tmp_path)

    assert len(asked) == prompts
    if len(asked) == 2:
        assert "auth for claude" in asked[1]
    config = tmp_path / ".booley" / "booley.toml"
    if written is None:
        assert result == init_cmd.StepResult("agent_config", "err", "agent selection aborted")
        assert not config.exists()
    else:
        assert config.read_bytes() == written
    if len(answers) == 3:
        assert (
            "agent provider selection aborted after 3 invalid answers" in capsys.readouterr().out
        )


@pytest.mark.parametrize(
    ("body", "credentials", "label"),
    [
        pytest.param(
            b'[agent]\nprovider = "codex"\nauth = "subscription"\n',
            (),
            "codex/subscription",
            id="provider-alias",
        ),
        pytest.param(
            b'[agent]\nclis = ["claude", "codex"]\ndefault = "codex"\n\n'
            b'[agent.auth]\nclaude = "api_key"\ncodex = "subscription"\n',
            (),
            "codex/subscription",
            id="per-cli-table",
        ),
        pytest.param(
            b'[agent]\ndefault = "claude"\nauth = "subscription"\n',
            ("key",),
            "claude/subscription",
            id="default-auth-with-key",
        ),
    ],
)
def test_recorded_selection_is_kept_without_prompting(
    tmp_path, monkeypatch, body, credentials, label
):
    """A prompt would abort: the recorded selection is authoritative and not rewritten."""
    if "key" in credentials:
        api_key(monkeypatch, "claude")
    config = project_config(tmp_path, body)

    result = _select_at_terminal(tmp_path)

    assert result == init_cmd.StepResult("agent_config", "skip", label)
    assert config.read_bytes() == body


@pytest.mark.parametrize(
    ("result", "records"),
    [
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox", _SANDBOX_IMAGE, init_cmd.ImageLifecycleStatus.EXTERNAL
            ),
            [("project_image", "skip", "user-managed image")],
            id="external",
        ),
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox", _SANDBOX_IMAGE, init_cmd.ImageLifecycleStatus.STALE
            ),
            [("docker_image", "warn", "Sandbox Image provenance is stale")],
            id="stale",
        ),
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox", _SANDBOX_IMAGE, init_cmd.ImageLifecycleStatus.CURRENT
            ),
            [("docker_image", "current", "current")],
            id="current",
        ),
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox-riscv",
                _SANDBOX_IMAGE,
                init_cmd.ImageLifecycleStatus.CHANGED,
                cleanup=ImageCleanup(removed=("ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.5",)),
            ),
            [("docker_image", "ok", "removed obsolete Sandbox Image tags")],
            id="release-tags-removed",
        ),
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox-riscv",
                _SANDBOX_IMAGE,
                init_cmd.ImageLifecycleStatus.CURRENT,
                cleanup=ImageCleanup(pending=("ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.5",)),
            ),
            [("docker_image", "warn", "obsolete Sandbox Image tags remain")],
            id="cleanup-pending",
        ),
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox-riscv",
                _SANDBOX_IMAGE,
                init_cmd.ImageLifecycleStatus.STALE,
                diagnostics=(Diagnostic("stale", "flavor provenance is stale"),),
                cleanup=ImageCleanup(
                    pending=("ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.5",),
                    retained_required=("ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.4",),
                ),
            ),
            [
                ("docker_image", "warn", "obsolete Sandbox Image tags remain"),
                ("docker_image", "warn", "obsolete Sandbox Image tags are in use"),
                ("docker_image", "warn", "Sandbox Image provenance is stale"),
            ],
            id="stale-with-cleanup",
        ),
        pytest.param(
            init_cmd.LifecycleResult(
                "booley-sandbox-riscv",
                _SANDBOX_IMAGE,
                init_cmd.ImageLifecycleStatus.CHANGED,
                changed_images=("booley-sandbox-riscv",),
                cleanup=ImageCleanup(removed=("ghcr.io/boldaxolotl/booley-sandbox-riscv:0.2.5",)),
            ),
            [
                ("docker_image", "ok", "removed obsolete Sandbox Image tags"),
                ("docker_image", "ok", "selected booley-sandbox-riscv"),
            ],
            id="rebuilt",
        ),
    ],
)
def test_image_lifecycle_step_records(tmp_path, monkeypatch, result, records):
    monkeypatch.setattr(
        init_cmd.image_lifecycle, "reconcile_planned", lambda *_args, **_kwargs: result
    )
    # A changed image probes live Sandboxes through the real Docker CLI.
    monkeypatch.setattr(sr, "sessions_on_stale_image", lambda _root, _image: [])
    ctx = InitContext(project_root=tmp_path)

    # private: every caller goes on to Sandbox Issuance, which needs Docker.
    assert init_cmd._step_image_lifecycle(ctx) is result
    assert [(r.name, r.status, r.detail) for r in ctx.results] == records


def test_unchanged_sandbox_is_recorded_as_current(tmp_path):
    ctx = InitContext(project_root=tmp_path)

    # private: only `_step_sandbox` reports Sandbox changes, after a Sandbox Issuance
    # that needs Docker.
    init_cmd._report_sandbox_changes(ctx, None, SimpleNamespace(changed=False))

    assert ctx.results[-1] == init_cmd.StepResult("interactive", "current", "current")
