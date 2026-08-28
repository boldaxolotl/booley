"""Compatibility contract delegated to the supported Claude Agent SDK interface."""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path


def test_production_code_uses_only_public_claude_sdk_imports() -> None:
    private_imports: list[str] = []
    for path in Path("src/booley").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "claude_agent_sdk._"
            ):
                private_imports.append(f"{path}:{node.lineno}: {node.module}")
            elif isinstance(node, ast.Import):
                private_imports.extend(
                    f"{path}:{node.lineno}: {alias.name}"
                    for alias in node.names
                    if alias.name.startswith("claude_agent_sdk._")
                )

    assert not private_imports, "private Claude SDK imports:\n" + "\n".join(private_imports)


def test_windows_batch_cli_fails_closed_without_execution(tmp_path) -> None:
    sentinel = tmp_path / "batch-executed"
    batch_cli = tmp_path / "claude.cmd"
    batch_cli.write_text(f'@echo off\ntype nul > "{sentinel}"\n', encoding="utf-8")
    script = textwrap.dedent(
        """
        import platform
        import sys

        import anyio
        from claude_agent_sdk import CLIConnectionError, ClaudeAgentOptions, query

        platform.system = lambda: "Windows"

        async def main():
            try:
                async for _message in query(
                    prompt="compatibility probe",
                    options=ClaudeAgentOptions(cli_path=sys.argv[1]),
                ):
                    pass
            except CLIConnectionError as exc:
                assert "Refusing to execute batch script" in str(exc), exc
                return
            raise AssertionError("Windows batch CLI was not rejected")

        anyio.run(main)
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", script, str(batch_cli)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists()
