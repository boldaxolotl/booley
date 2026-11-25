"""The one fake of the Lint Flow's linter command and FuseSoC setup, for whole ``booley flow lint`` runs."""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from booley.flows.base import FlowMechanics, SubprocessResult
from booley.flows.lint.flow import LintFlow
from booley.fusesoc import fusesoc_registry
from booley.runtime.endpoint_execution import ExecutionResult
from tests.canned_process_support import canned_command
from tests.subprocess_budget import run_bounded_child

LintRun = Callable[..., tuple[ExecutionResult, list[list[str]]]]
# Captured before any test replaces them.
_REAL_RESOLVE = fusesoc_registry.resolve_target_handle
_REAL_SETUP_COMMAND = fusesoc_registry.setup_command_for_handle


def stub_resolved(
    eda_tool: str | None = "verilator",
    files: tuple[str, ...] = (),
    build_root: Path = Path("build"),
    toplevel: str = "",
) -> object:
    """Minimal ResolvedTarget standing for what ``resolve_target_handle`` returns.

    An empty toplevel skips the toplevel check. ``files`` declares linted
    SystemVerilog sources (EDAM ``name``s).
    """
    return fusesoc_registry.ResolvedTarget(
        name="stub",
        vlnv="::stub_demo:0",
        toplevel=toplevel,
        eda_tool=eda_tool,
        configured_eda_tool=eda_tool,
        files=tuple(
            fusesoc_registry.ResolvedFile(name=name, file_type="systemVerilogSource")
            for name in files
        ),
        parameters={},
        build_root=build_root,
        edam_path=Path("build/stub_demo_0.eda.yml"),
    )


def install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LintRun:
    """Run ``booley flow lint`` through its CLI entry with a canned linter subprocess.

    ``run(*processes, args=(), target="lite", work_dir=None, resolved=...)``
    returns the result and the command of every linter run. The Targets run in
    order and take the processes in order (the last repeats). ``resolved(handle,
    build_root)`` stands for ``resolve_target_handle``; ``resolved=None`` keeps
    the resolver the test installed. ``measured=True`` runs each canned result as a
    real child process through the measured run, so a history record is written.
    """
    monkeypatch.setenv("BOOLEY_CONTAINER", "1")
    monkeypatch.setenv("BOOLEY_LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.delenv("BOOLEY_STATE_FILE", raising=False)

    def run(
        *processes: SubprocessResult,
        args: tuple[str, ...] = (),
        target: str = "lite",
        work_dir: Path | None = None,
        resolved: Callable[..., object] | None = lambda handle, build_root: stub_resolved(
            build_root=tmp_path / handle.selector
        ),
        measured: bool = False,
    ) -> tuple[ExecutionResult, list[list[str]]]:
        commands: list[list[str]] = []

        def execute(_flow: LintFlow, cmd: list[str], **_kwargs: object) -> SubprocessResult:
            commands.append(cmd)
            process = processes[min(len(commands), len(processes)) - 1]
            if measured:
                # The measured run that ``_execute`` wraps, on a real child: no public
                # name runs one command and writes its history record.
                return FlowMechanics._execute_local(_flow, canned_command(process))
            return process

        # Unit tests have no Verilator: the linter subprocess is the one
        # private seam the public Flow entry cannot replace.
        monkeypatch.setattr(LintFlow, "_execute", execute)
        if resolved is not None:
            # FuseSoC setup is a command too: a test names what it resolved.
            monkeypatch.setattr(
                fusesoc_registry,
                "resolve_target_handle",
                lambda handle, build_root, **_kwargs: resolved(handle, build_root),
            )
        argv = ["--target", target, "--work-dir", str(work_dir or tmp_path), *args]
        return LintFlow().execute_cli(argv), commands

    return run


def isolated_verible_fusesoc(root: Path, spelling: str) -> list[str]:
    """A FuseSoC command whose Edalize finds Booley's Verible tool under either spelling."""
    import booley

    nodes = root / "nodes"
    nodes.mkdir()
    source = Path(booley.__file__).parent / "data/edalize/verible.py"
    for name in ("verible", "veriblelint"):
        shutil.copyfile(source, nodes / f"{name}.py")
    script = (
        "import edalize.tools; "
        f"edalize.tools.__path__ = [{str(nodes)!r}, *edalize.tools.__path__]; "
        "from edalize.flows.lint import Lint; "
        f"options = Lint.get_tool_options({{'tool': {spelling!r}}}); "
        "assert 'rules' in options and 'ruleset' in options; "
        "from fusesoc.main import main; main()"
    )
    return [sys.executable, "-c", script]


def real_fusesoc_setup(monkeypatch: pytest.MonkeyPatch, command: list[str], root: Path) -> None:
    """Run the real FuseSoC setup as *command*, isolated from the host's FuseSoC configuration."""
    real_resolve = _REAL_RESOLVE
    monkeypatch.setattr(fusesoc_registry, "setup_command_for_handle", _REAL_SETUP_COMMAND)
    config = root / "isolated-fusesoc.conf"
    config.write_text("[main]\n", encoding="utf-8")
    command = [*command, "--config", str(config)]
    env = dict(os.environ)
    env.pop("FUSESOC_CORES", None)
    for kind in ("CONFIG", "CACHE", "DATA"):
        env[f"XDG_{kind}_HOME"] = str(root / f"isolated-{kind.lower()}")

    def bounded_runner(*args, **kwargs):
        assert set(kwargs) <= {
            "cwd",
            "env",
            "capture_output",
            "text",
        }  # the helper captures text itself
        assert kwargs["env"] == env
        assert args[0][args[0].index("--config") + 1] == str(config)
        return run_bounded_child(args[0], cwd=kwargs.get("cwd"), env=kwargs["env"])

    def resolve(*args, **kwargs):
        return real_resolve(
            *args, **{**kwargs, "fusesoc_cmd": command, "runner": bounded_runner, "env": env}
        )

    monkeypatch.setattr(fusesoc_registry, "resolve_target_handle", resolve)
