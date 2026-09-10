"""Ticket execution always uses the Console, independent of terminal detection."""

from __future__ import annotations

import argparse
import sys
from unittest.mock import AsyncMock, Mock

import pytest

from booley.harness import __main__ as child
from booley.harness import booley as parent
from booley.harness import developer, terminal


@pytest.mark.parametrize("flag", ["--no-console", "-L"])
def test_run_rejects_removed_log_mode(flag):
    with pytest.raises(SystemExit) as error:
        parent._build_parser().parse_args(["run", flag])
    assert error.value.code == 2


def test_harness_rejects_removed_log_mode(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["harness", "--no-console"])
    with pytest.raises(SystemExit) as error:
        child._parse_args()
    assert error.value.code == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "environment",
    [{}, {"BOOLEY_CONSOLE": "0"}, {"NO_COLOR": "1"}, {"TERM": "dumb"}],
)
@pytest.mark.parametrize("tty", [True, False])
async def test_ticket_execution_always_uses_console(environment, tty, tmp_path, monkeypatch):
    for name in ("BOOLEY_CONSOLE", "NO_COLOR", "TERM"):
        monkeypatch.delenv(name, raising=False)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(sys, "stdout", Mock(isatty=Mock(return_value=tty)))
    console_run = AsyncMock(return_value=None)
    monkeypatch.setattr(developer, "_run_with_console", console_run)

    assert parent._will_use_console(argparse.Namespace()) is True
    await developer.run_ticket("demo", tmp_path)
    console_run.assert_awaited_once_with("demo", tmp_path, True)


@pytest.mark.asyncio
async def test_console_startup_failure_propagates_without_executing_ticket(tmp_path, monkeypatch):
    from booley.harness.console import app as console

    app = Mock(run_async=AsyncMock(side_effect=RuntimeError("Console failed")))
    monkeypatch.setattr(console, "ConsoleApp", Mock(return_value=app))
    prepare = AsyncMock()
    monkeypatch.setattr(developer, "_prepare_ticket", prepare)

    with pytest.raises(RuntimeError, match="Console failed"):
        await developer.run_ticket("demo", tmp_path)

    prepare.assert_not_awaited()
    assert terminal.get_console_app() is None


@pytest.mark.asyncio
async def test_console_worker_surfaces_preflight_failure(tmp_path, monkeypatch):
    from booley.harness.console.app import ConsoleApp
    from booley.harness.ticket_preflight import TicketPreflightError

    run_async = ConsoleApp.run_async

    async def run_headless(app):
        await run_async(app, headless=True)

    monkeypatch.setattr(ConsoleApp, "run_async", run_headless)
    prepare = AsyncMock(side_effect=TicketPreflightError(["missing toolchain"]))
    monkeypatch.setattr(developer, "_prepare_ticket", prepare)

    with pytest.raises(TicketPreflightError, match="missing toolchain"):
        await developer.run_ticket("demo", tmp_path)

    prepare.assert_awaited_once_with("demo", tmp_path, True)
    assert terminal.get_console_app() is None


def test_console_lifecycle_failure_returns_cli_error(tmp_path, monkeypatch):
    from booley.harness.console.app import ConsoleApp

    run_async = ConsoleApp.run_async

    async def run_headless(app):
        await run_async(app, headless=True)

    def fail_mount(app):
        raise RuntimeError("Console mount failed")

    monkeypatch.setattr(ConsoleApp, "run_async", run_headless)
    monkeypatch.setattr(ConsoleApp, "on_mount", fail_mount)
    prepare = AsyncMock()
    monkeypatch.setattr(developer, "_prepare_ticket", prepare)
    args = argparse.Namespace(ticket="demo", no_transcripts=True)

    assert child._run_harness(args, tmp_path) == 1
    prepare.assert_not_awaited()
    assert terminal.get_console_app() is None
