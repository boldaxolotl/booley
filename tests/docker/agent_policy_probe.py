"""Exercise the installed agent CLIs and their managed web-isolation policies.

A local server fakes the model provider API the agent CLIs call.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path
from typing import Any

# Standalone script run as /work/tests/docker/agent_policy_probe.py: put the repository
# root on sys.path so the shared time-budget policy imports, and the CI scripts folder so
# the shared fake model API does.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / ".github/scripts"))

from fake_model_api import FakeModelApi
from tests.time_budget import hang_guard_s

from booley.runtime import dockerfile_syntax

# Hang guard for every wait here: nothing asserts a command or thread expires.
_HANG_S = hang_guard_s()

# The repository manifest is the single source of the pinned CLI versions. Reading
# it (CI mounts the checkout at /work) checks the image against the current pins.
_MANIFEST = Path(__file__).resolve().parents[2] / "src/booley/data/docker/agent-clis-package.json"
_PINS = json.loads(_MANIFEST.read_text(encoding="utf-8"))
EXPECTED_CLAUDE = _PINS["dependencies"]["@anthropic-ai/claude-code"]
EXPECTED_CODEX = _PINS["dependencies"]["@openai/codex"]
EXPECTED_T3CODE = _PINS["t3code"]["version"]
T3CODE_POLICY = Path("/etc/t3code/policy.json")
CLAUDE_WEB_TOOLS = {"WebFetch", "WebSearch"}
CODEX_POLICY_SOURCE = "/etc/codex/requirements.toml"
CANARY_TEXT = "booley-agent-policy-canary"


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = _HANG_S,
) -> subprocess.CompletedProcess[str]:
    # Unbounded child: standalone script run inside the image, outside pytest
    result = subprocess.run(
        command, cwd=cwd, env=env, text=True, capture_output=True, timeout=timeout, check=False
    )
    if result.returncode != 0:
        output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        raise AssertionError(f"command failed ({result.returncode}): {command!r}\n{output}")
    return result


def _recipe_versions() -> tuple[str, str]:
    """Return the Node and npm versions pinned by the image's packaged recipe."""
    recipe = resources.files("booley").joinpath("data/docker/Dockerfile.base")
    contents = recipe.read_text(encoding="utf-8")
    return (
        dockerfile_syntax.argument_default(contents, "NODE_VERSION"),
        dockerfile_syntax.argument_default(contents, "NPM_VERSION"),
    )


def _assert_versions(expected_node: str, expected_npm: str) -> dict[str, str]:
    versions = {
        "node": _run(["node", "--version"]).stdout.strip().removeprefix("v"),
        "npm": _run(["npm", "--version"]).stdout.strip(),
        "claude": _run(["claude", "--version"]).stdout.split()[0],
        "codex": _run(["codex", "--version"]).stdout.split()[1],
        "t3": _run(["t3", "--version"]).stdout.split()[1].removeprefix("v"),
    }
    expected = {
        "node": expected_node,
        "npm": expected_npm,
        "claude": EXPECTED_CLAUDE,
        "codex": EXPECTED_CODEX,
        "t3": EXPECTED_T3CODE,
    }
    assert versions == expected, f"runtime versions differ: {versions!r} != {expected!r}"
    return versions


def _assert_t3code_policy_is_read_only() -> dict[str, Any]:
    """The Sandbox user finds the policy as shipped and cannot change it or its directory."""
    shipped = _MANIFEST.with_name("t3code-policy.json").read_bytes()
    assert T3CODE_POLICY.read_bytes() == shipped, "installed policy differs from the repository's"
    owners = {
        name: (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        for name, info in (
            ("directory", T3CODE_POLICY.parent.stat()),
            ("file", T3CODE_POLICY.stat()),
        )
    }
    assert owners == {"directory": (0, 0, 0o755), "file": (0, 0, 0o644)}, owners
    refused: list[str] = []
    # The spare lives on the container's root filesystem, like /etc: a rename
    # across mounts fails with EXDEV before any permission check.
    with tempfile.TemporaryDirectory(dir="/tmp") as spare_dir:
        spare = Path(spare_dir) / "spare.json"
        spare.write_bytes(shipped)
        attempts: dict[str, Callable[[], object]] = {
            "modify": lambda: T3CODE_POLICY.open("ab"),
            "chmod": lambda: T3CODE_POLICY.chmod(0o666),
            "replace": lambda: spare.replace(T3CODE_POLICY),
            "delete": T3CODE_POLICY.unlink,
            "create": lambda: (T3CODE_POLICY.parent / "extra.json").open("xb"),
        }
        for action, attempt in attempts.items():
            try:
                attempt()
            except PermissionError:
                refused.append(action)
            else:
                raise AssertionError(f"the Sandbox user could {action} the T3 Code policy")
    assert T3CODE_POLICY.read_bytes() == shipped
    return {"owners": owners, "refused": refused}


def _assert_diagnostics() -> None:
    _run(["claude", "--help"])
    _run(["claude", "doctor"])
    _run(["codex", "--help"])
    # Unbounded child: standalone script run inside the image, outside pytest
    codex = subprocess.run(
        ["codex", "doctor", "--json"], text=True, capture_output=True, timeout=_HANG_S, check=False
    )
    report = json.loads(codex.stdout)
    assert report["codexVersion"] == EXPECTED_CODEX
    assert report["checks"]["config.load"]["summary"] == "config loaded"


def _walk_installed_tree(tree: dict[str, Any]) -> dict[str, str]:
    installed: dict[str, str] = {}
    pending = [tree]
    while pending:
        package = pending.pop()
        for name, dependency in package.get("dependencies", {}).items():
            if not dependency:
                continue
            version = dependency.get("version")
            assert isinstance(version, str), f"installed package has no version: {name}"
            installed[name] = version
            pending.append(dependency)
    return installed


def _assert_npm_tree() -> dict[str, str]:
    result = _run(["npm", "ls", "--prefix", "/opt/agent-clis", "--omit=dev", "--all", "--json"])
    installed = _walk_installed_tree(json.loads(result.stdout))
    lock = json.loads(Path("/opt/agent-clis/package-lock.json").read_text(encoding="utf-8"))
    locked = lock["packages"]
    assert installed["@anthropic-ai/claude-code"] == EXPECTED_CLAUDE
    assert installed["@openai/codex"] == EXPECTED_CODEX
    for name, version in installed.items():
        entry = locked[f"node_modules/{name}"]
        assert entry["version"] == version, f"installed {name} differs from package lock"
        assert entry["integrity"].startswith("sha512-"), f"{name} has no locked integrity"
    return dict(sorted(installed.items()))


def _assert_sdk_uses_system_claude() -> str:
    script = """
import anyio
from claude_agent_sdk import ClaudeAgentOptions, query

async def main():
    try:
        async for _ in query(
            prompt="probe",
            options=ClaudeAgentOptions(),
        ):
            pass
    except Exception as exc:
        text = str(exc)
        assert "CLI not found" not in text and "bundled" not in text.lower(), text

anyio.run(main)
"""
    import claude_agent_sdk

    bundle = Path(claude_agent_sdk.__file__).parent / "_bundled" / "claude"
    assert not bundle.exists(), f"SDK bundled CLI remains: {bundle}"
    assert Path("/usr/local/bin/claude").resolve().is_file()
    _run(["python", "-c", script], timeout=_HANG_S)
    return str(Path("/usr/local/bin/claude").resolve())


def _assert_booley_configuration(root: Path) -> dict[str, str]:
    from booley.harness.web_isolation import policy_error
    from booley.runtime import incontainer_setup

    statuses: dict[str, str] = {}
    for app in ("claude", "codex"):
        home = root / f"booley-{app}-home"
        statuses[app] = incontainer_setup.register(app, home=home)
    codex = (root / "booley-codex-home" / ".codex" / "config.toml").read_text(encoding="utf-8")
    claude = json.loads(
        (root / "booley-claude-home" / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    assert 'web_search = "disabled"' in codex
    assert set(claude["permissions"]["deny"]) >= CLAUDE_WEB_TOOLS
    assert policy_error() is None
    return statuses


def _claude_environment(home: Path, model_api: FakeModelApi) -> dict[str, str]:
    return {**os.environ, "HOME": str(home), **model_api.claude_environment()}


def _contains_canary_result(requests: list[dict[str, Any]]) -> bool:
    return any(CANARY_TEXT in json.dumps(request.get("messages", [])) for request in requests)


def _run_claude_case(case: str, root: Path, extra_args: list[str]) -> list[str]:
    home = root / f"claude-home-{case}"
    project = root / f"claude-project-{case}"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (project / ".claude").mkdir(parents=True, exist_ok=True)
    canary = project / "canary.txt"
    canary.write_text(CANARY_TEXT, encoding="utf-8")
    command = [
        "claude",
        "--print",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
        *extra_args,
        "Read canary.txt, then search the web and fetch https://example.com.",
    ]
    with FakeModelApi([("Read", {"file_path": str(canary)})]) as provider:
        _run(command, cwd=project, env=_claude_environment(home, provider), timeout=_HANG_S)
    assert provider.requests, f"Claude {case} sent no model request"
    tools = {tool["name"] for tool in provider.requests[0].get("tools", [])}
    assert "Read" in tools, f"Claude {case} omitted Read canary: {sorted(tools)}"
    assert tools.isdisjoint(CLAUDE_WEB_TOOLS), f"Claude {case} exposed web tools: {sorted(tools)}"
    assert _contains_canary_result(provider.requests), f"Claude {case} did not return Read output"
    return sorted(tools)


def _assert_claude_policy(root: Path) -> dict[str, list[str]]:
    allow = json.dumps({"permissions": {"allow": ["Read", "WebFetch", "WebSearch"]}})
    user_settings = root / "claude-home-user" / ".claude" / "settings.json"
    user_settings.parent.mkdir(parents=True)
    user_settings.write_text(allow, encoding="utf-8")
    project_settings = root / "claude-project-project" / ".claude" / "settings.json"
    project_settings.parent.mkdir(parents=True)
    project_settings.write_text(allow, encoding="utf-8")
    cases = {
        "user": [],
        "project": [],
        "cli_allow": ["--allowedTools=Read,WebFetch,WebSearch"],
        "bypass": ["--dangerously-skip-permissions"],
        "web_prompt": [],
    }
    return {case: _run_claude_case(case, root, args) for case, args in cases.items()}


def _codex_warning(report: dict[str, Any]) -> str:
    details = report["checks"]["config.load"]["details"]
    warning = details.get("startup warning", "")
    assert "falling back to required value Disabled" in warning, warning
    assert CODEX_POLICY_SOURCE in warning, warning
    return warning


def _run_codex_case(case: str, root: Path, extra_args: list[str]) -> str:
    home = root / f"codex-home-{case}"
    project = root / f"codex-project-{case}"
    home.mkdir(parents=True, exist_ok=True)
    project.mkdir(parents=True, exist_ok=True)
    codex_home = home / ".codex"
    codex_home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({"HOME": str(home), "CODEX_HOME": str(codex_home)})
    # Unbounded child: standalone script run inside the image, outside pytest
    result = subprocess.run(
        ["codex", *extra_args, "doctor", "--json"],
        cwd=project,
        env=env,
        text=True,
        capture_output=True,
        timeout=_HANG_S,
        check=False,
    )
    report = json.loads(result.stdout)
    assert report["codexVersion"] == EXPECTED_CODEX
    try:
        return _codex_warning(report)
    except AssertionError as exc:
        raise AssertionError(f"Codex {case} policy diagnostic was incomplete: {exc}") from exc


def _assert_codex_policy(root: Path) -> dict[str, str]:
    user_home = root / "codex-home-user" / ".codex"
    user_home.mkdir(parents=True)
    (user_home / "config.toml").write_text('web_search = "live"\n', encoding="utf-8")
    project_config = root / "codex-project-project" / ".codex" / "config.toml"
    project_config.parent.mkdir(parents=True)
    project_config.write_text('web_search = "live"\n', encoding="utf-8")
    project_root = project_config.parents[1]
    _run(["git", "init", "-q"], cwd=project_root)
    project_home = root / "codex-home-project" / ".codex"
    project_home.mkdir(parents=True)
    (project_home / "config.toml").write_text(
        f'[projects."{project_root}"]\ntrust_level = "trusted"\n', encoding="utf-8"
    )
    cases = {
        "user": [],
        "project": [],
        "cli_config": ["-c", 'web_search="live"'],
        "search_flag": ["--search"],
        "danger_full_access": ["--dangerously-bypass-approvals-and-sandbox"],
    }
    return {case: _run_codex_case(case, root, args) for case, args in cases.items()}


def _assert_codex_hooks_pinned(root: Path) -> dict[str, str]:
    """No user layer turns off Codex's hooks, which run the managed deny hook (#5788)."""
    # Every layer also turns off the unpinned fast_mode: false there shows Codex applied it.
    off = "[features]\nhooks = false\nfast_mode = false\n"
    project = root / "codex-hooks-repo"
    (project / ".codex").mkdir(parents=True)
    (project / ".codex" / "config.toml").write_text(off, encoding="utf-8")
    _run(["git", "init", "-q"], cwd=project)
    # Each case: the user config and the command line arguments.
    cases: dict[str, tuple[str, list[str]]] = {
        "user": (off, []),
        "legacy_alias": ("[features]\ncodex_hooks = false\nfast_mode = false\n", []),
        "project": (f'[projects."{project}"]\ntrust_level = "trusted"\n', []),
        "cli_config": ("", ["-c", "features.hooks=false", "-c", "features.fast_mode=false"]),
        "cli_disable": ("", ["--disable", "hooks", "--disable", "fast_mode"]),
    }
    states: dict[str, str] = {}
    for case, (config, args) in cases.items():
        codex_home = root / f"codex-hooks-{case}" / ".codex"
        codex_home.mkdir(parents=True)
        (codex_home / "config.toml").write_text(config, encoding="utf-8")
        env = {**os.environ, "HOME": str(codex_home.parent), "CODEX_HOME": str(codex_home)}
        listed = _run(["codex", *args, "features", "list"], cwd=project, env=env).stdout
        rows = {line.split()[0]: line.split()[-1] for line in listed.splitlines() if line}
        found = (rows["hooks"], rows["fast_mode"])
        assert found == ("true", "false"), f"Codex {case} hooks and fast_mode: {listed}"
        states[case] = rows["hooks"]
    return states


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    assert os.geteuid() != 0, "probe must run as the image's unprivileged agent user"
    with tempfile.TemporaryDirectory(prefix="booley-agent-policy-") as temporary:
        root = Path(temporary)
        evidence = {
            "versions": _assert_versions(*_recipe_versions()),
            "t3code_policy": _assert_t3code_policy_is_read_only(),
            "installed_packages": _assert_npm_tree(),
            "sdk_cli": _assert_sdk_uses_system_claude(),
            "booley_configuration": _assert_booley_configuration(root),
            "codex_policy": _assert_codex_policy(root),
            "codex_hooks": _assert_codex_hooks_pinned(root),
            "claude_tools": _assert_claude_policy(root),
        }
        _assert_diagnostics()
    rendered = json.dumps(evidence, indent=2, sort_keys=True)
    if args.evidence is not None:
        args.evidence.parent.mkdir(parents=True, exist_ok=True)
        args.evidence.write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
