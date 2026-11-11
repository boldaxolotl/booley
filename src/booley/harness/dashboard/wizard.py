"""The Dashboard's Run wizard: worktree, Flow, arguments, then review and submit.

One modal screen shows one stage at a time. It dismisses with the new Job's row
key once submitted, or ``None`` when the human backs out of the first stage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option
from textual.worker import Worker, WorkerState

from booley.harness.dashboard.launch import (
    ArgumentsError,
    Launcher,
    shell_command,
    split_arguments,
)
from booley.harness.dashboard.targets import WorktreeChoice

CHECK_GROUP = "run-check"
LOADING = "Loading worktrees…"
NO_WORKTREES = "No worktrees to run in"
SUBMIT, COPY = "Submit", "Copy command"
COPY_NOTICE = "Asked the terminal to copy the command (some terminals refuse)"
# The key footer of each stage, in order; Esc goes one stage back.
_STAGES: dict[str, str] = {
    "worktree": "↑↓ move · ⏎ choose · esc close",
    "flow": "↑↓ move · ⏎ choose · esc back",
    "form": "type the arguments · ⏎ check · esc back",
    "review": "↑↓ choose · ⏎ apply · esc back",
}
_ORDER = tuple(_STAGES)


@dataclass(frozen=True)
class _Request:
    """What the human asked to run; a check answers exactly one."""

    worktree: Path
    flow: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _Checked:
    request: _Request
    error: str


class RunWizard(ModalScreen[str | None]):
    """Choose where and what to run, check the arguments, then submit or copy the command."""

    BINDINGS: ClassVar = [Binding("escape", "back", show=False)]
    DEFAULT_CSS = """
    RunWizard { align: center middle; }
    #run-box { width: 90%; max-width: 100; height: auto; max-height: 90%;
               border: round $accent; padding: 0 1; background: $surface; }
    #run-box OptionList { height: auto; max-height: 12; }
    #run-footer { color: $text-muted; }
    #run-error { color: $error; height: auto; }
    """

    def __init__(
        self,
        launcher: Launcher,
        choices: tuple[WorktreeChoice, ...],
        message: str,
        preselected: Path | None,
    ) -> None:
        super().__init__()
        self._launcher = launcher
        self._flows = launcher.flows()
        self._choices = choices
        self._message = message
        self._preselected = preselected
        self._stage = "worktree"
        self._worktree: Path | None = None
        self._flow = ""
        self._argv: tuple[str, ...] = ()
        self._checking: _Request | None = None

    def compose(self) -> ComposeResult:
        with Vertical(id="run-box"):
            yield Static(id="run-title", markup=False)
            yield OptionList(id="run-worktrees", markup=False)
            yield OptionList(
                *(Option(f"{flow.name} — {flow.blurb}") for flow in self._flows),
                id="run-flows",
                markup=False,
            )
            yield Input(id="run-arguments", placeholder="arguments, e.g. --target NAME")
            yield Static(id="run-command", markup=False)
            yield OptionList(SUBMIT, COPY, id="run-actions", markup=False)
            yield Static(id="run-error", markup=False)
            yield Static(id="run-footer", markup=False)

    def on_mount(self) -> None:
        self.set_worktrees(self._choices, self._message)
        self._show_stage("worktree")

    def set_worktrees(self, choices: tuple[WorktreeChoice, ...], message: str) -> None:
        """Replace the worktree list (``message`` stands in while there is none), keeping the highlight."""
        options = self.query_one("#run-worktrees", OptionList)
        kept = self._choices[options.highlighted].path if options.highlighted is not None else None
        self._choices = choices
        options.clear_options()
        if not choices:
            options.add_option(Option(message or NO_WORKTREES, disabled=True))
            return
        options.add_options([Option(choice.label) for choice in choices])
        wanted = kept or self._preselected
        options.highlighted = next(
            (index for index, choice in enumerate(choices) if choice.path == wanted), 0
        )

    def _title(self) -> str:
        titles = {
            "worktree": "Run a Booley Flow: choose the worktree",
            "flow": "Run a Booley Flow: choose the Flow",
            "form": f"booley flow {self._flow} in {self._worktree.name if self._worktree else ''}",
            "review": "Run a Booley Flow: review the command",
        }
        return titles[self._stage]

    def _show_stage(self, stage: str) -> None:
        """Show *stage* only, give it the keyboard and clear the last stage's error."""
        self._stage = stage
        self._checking = None
        shown: dict[str, tuple[str, ...]] = {
            "worktree": ("#run-worktrees",),
            "flow": ("#run-flows",),
            "form": ("#run-arguments",),
            "review": ("#run-command", "#run-actions"),
        }
        for widget_id in {i for ids in shown.values() for i in ids}:
            self.query_one(widget_id, Widget).display = widget_id in shown[stage]
        self.query_one("#run-title", Static).update(self._title())
        self.query_one("#run-error", Static).update("")
        self.query_one("#run-footer", Static).update(_STAGES[stage])
        self.query_one(shown[stage][-1], Widget).focus()

    def action_back(self) -> None:
        index = _ORDER.index(self._stage)
        if index == 0:
            self.dismiss(None)
        else:
            self._show_stage(_ORDER[index - 1])

    def _show_error(self, text: str) -> None:
        self.query_one("#run-error", Static).update(text)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if event.option_list.id == "run-worktrees":
            self._worktree = self._choices[index].path
            self._show_stage("flow")
        elif event.option_list.id == "run-flows":
            flow = self._flows[index].name
            if flow != self._flow:  # the arguments belong to one Flow
                self.query_one("#run-arguments", Input).value = ""
            self._flow = flow
            self._show_stage("form")
        else:
            self._apply_action(SUBMIT if index == 0 else COPY)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        assert self._worktree is not None
        try:
            argv = split_arguments(event.value)
        except ArgumentsError as exc:
            self._show_error(str(exc))
            return
        request = _Request(self._worktree, self._flow, argv)
        self._checking = request
        self._show_error("Checking…")
        self.run_worker(
            lambda: _Checked(
                request, self._launcher.check(request.worktree, request.flow, request.argv)
            ),
            thread=True,
            exclusive=True,
            group=CHECK_GROUP,
            exit_on_error=False,
        )

    def on_input_changed(self, _event: Input.Changed) -> None:
        """Editing the arguments drops a check still running for the old text."""
        self._checking = None

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply a finished check unless the human moved on or changed the arguments meanwhile."""
        if event.state is not WorkerState.SUCCESS or event.worker.group != CHECK_GROUP:
            return
        event.stop()
        checked = event.worker.result
        assert isinstance(checked, _Checked)
        if checked.request != self._checking:
            return
        if checked.error:
            self._checking = None
            self._show_error(checked.error)
            return
        self._argv = checked.request.argv
        self._show_stage("review")
        self.query_one("#run-command", Static).update(
            shell_command(checked.request.worktree, checked.request.flow, self._argv)
        )

    def _apply_action(self, action: str) -> None:
        assert self._worktree is not None
        command = shell_command(self._worktree, self._flow, self._argv)
        if action == COPY:
            self.app.copy_to_clipboard(command)
            self.notify(COPY_NOTICE)
            return
        try:
            key = self._launcher.submit(self._worktree, self._flow, self._argv)
        except OSError as exc:
            self._show_error(f"Could not start the Job: {exc}")
            return
        self.dismiss(key)
