"""The Dashboard's Run wizard: worktree, Flow, its form, then review and submit.

One modal screen shows one stage at a time. The form stage is generated from the
Flow's human parser (``run_form``): a row per option, changed in a choice list or
a text editor stage, plus one extra-arguments row. It dismisses with the new
Job's row key once submitted, or ``None`` when the human backs out of the first
stage.
"""

from __future__ import annotations

from collections.abc import Callable
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

from booley.harness.dashboard.launch import ArgumentsError, Launcher, shell_command
from booley.harness.dashboard.run_form import (
    DEFAULT,
    NO_FORM,
    Field,
    FormError,
    RunForm,
    command_argv,
)
from booley.harness.dashboard.targets import WorktreeChoice

CHECK_GROUP = "run-check"
FORM_GROUP = "run-form"  # not "targets-": the app's own worker handler reacts to those
BUILDING = "Building the form…"
EXTRA_LABEL, REVIEW = "Extra arguments", "Review the command"
LOADING = "Loading worktrees…"
NO_WORKTREES = "No worktrees to run in"
SUBMIT, COPY = "Submit", "Copy command"
COPY_NOTICE = "Asked the terminal to copy the command (some terminals refuse)"
# The key footer of each stage.
_STAGES: dict[str, str] = {
    "worktree": "↑↓ move · ⏎ choose · esc close",
    "flow": "↑↓ move · ⏎ choose · esc back",
    "form": "↑↓ move · ⏎ change · esc back",
    "choice": "↑↓ move · ⏎ keep · esc cancel",
    "edit": "type the value · ⏎ keep · esc cancel",
    "review": "↑↓ choose · ⏎ apply · esc back",
}
# Esc goes to the parent stage; choosing or editing a value and checking the command return to the form.
_PARENT = {
    "flow": "worktree",
    "form": "flow",
    "choice": "form",
    "edit": "form",
    "review": "form",
}
# Which widgets each stage shows; the last one takes the keyboard.
_SHOWN: dict[str, tuple[str, ...]] = {
    "worktree": ("#run-worktrees",),
    "flow": ("#run-flows",),
    "form": ("#run-note", "#run-form"),
    "choice": ("#run-hint", "#run-choices"),
    "edit": ("#run-hint", "#run-edit"),
    "review": ("#run-command", "#run-actions"),
}


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
    """Choose where and what to run, fill in the form, then submit or copy the command it builds."""

    BINDINGS: ClassVar = [Binding("escape", "back", show=False)]
    DEFAULT_CSS = """
    RunWizard { align: center middle; }
    #run-box { width: 90%; max-width: 100; height: auto; max-height: 90%;
               border: round $accent; padding: 0 1; background: $surface; }
    #run-box OptionList { height: auto; max-height: 12; }
    #run-box #run-form { max-height: 16; }
    #run-footer { color: $text-muted; }
    #run-error { color: $error; height: auto; }
    #run-note, #run-hint { color: $text-muted; height: auto; }
    """

    def __init__(
        self,
        launcher: Launcher,
        choices: tuple[WorktreeChoice, ...],
        message: str,
        preselect: Callable[[tuple[WorktreeChoice, ...]], Path | None],
    ) -> None:
        super().__init__()
        self._launcher = launcher
        self._flows = launcher.flows()
        self._choices = choices
        self._message = message
        self._preselect = preselect
        self._stage = "worktree"
        self._worktree: Path | None = None
        self._flow = ""
        self._argv: tuple[str, ...] = ()
        self._checking: _Request | None = None
        # The form belongs to one (worktree, Flow); None while it is being built or if it failed.
        self._form_for: tuple[Path, str] | None = None
        self._form: RunForm | None = None
        self._form_worker: Worker[RunForm] | None = None  # the one build whose result counts
        self._building = False
        self._form_error = ""
        self._values: dict[str, str] = {}
        self._extra = ""
        self._changing = 0  # the form row a choice list or editor is changing

    def compose(self) -> ComposeResult:
        with Vertical(id="run-box"):
            yield Static(id="run-title", markup=False)
            yield OptionList(id="run-worktrees", markup=False)
            yield OptionList(
                *(Option(f"{flow.name} — {flow.blurb}") for flow in self._flows),
                id="run-flows",
                markup=False,
            )
            yield Static(id="run-note", markup=False)
            yield OptionList(id="run-form", markup=False)
            yield Static(id="run-hint", markup=False)
            yield OptionList(id="run-choices", markup=False)
            yield Input(id="run-edit", select_on_focus=False)
            yield Static(id="run-command", markup=False)
            yield OptionList(SUBMIT, COPY, id="run-actions", markup=False)
            yield Static(id="run-error", markup=False)
            yield Static(id="run-footer", markup=False)

    def on_mount(self) -> None:
        self.set_worktrees(self._choices, self._message)
        self._show_stage("worktree")

    def set_worktrees(self, choices: tuple[WorktreeChoice, ...], message: str) -> None:
        """Replace the worktree list (``message`` stands in while there is none), keeping the highlight.

        Until the human moves the highlight, the preselected worktree is worked out from the
        list as it is now: a list that arrives after the wizard opened still preselects.
        """
        options = self.query_one("#run-worktrees", OptionList)
        kept = (
            self._choices[options.highlighted].path
            if self._choices and options.highlighted is not None
            else None
        )
        self._choices = choices
        options.clear_options()
        if not choices:
            options.add_option(Option(message or NO_WORKTREES, disabled=True))
            return
        options.add_options([Option(choice.label) for choice in choices])
        wanted = kept or self._preselect(choices)
        options.highlighted = next(
            (index for index, choice in enumerate(choices) if choice.path == wanted), 0
        )

    def _title(self) -> str:
        titles = {
            "worktree": "Run a Booley Flow: choose the worktree",
            "flow": "Run a Booley Flow: choose the Flow",
            "form": f"booley flow {self._flow} in {self._worktree.name if self._worktree else ''}",
            "choice": f"booley flow {self._flow}: {self._row_label(self._changing)}",
            "edit": f"booley flow {self._flow}: {self._row_label(self._changing)}",
            "review": "Run a Booley Flow: review the command",
        }
        return titles[self._stage]

    def _show_stage(self, stage: str) -> None:
        """Show *stage* only, give it the keyboard and clear the last stage's error."""
        self._stage = stage
        self._checking = None
        for widget_id in {i for ids in _SHOWN.values() for i in ids}:
            self.query_one(widget_id, Widget).display = widget_id in _SHOWN[stage]
        if stage == "form":
            self._show_form()
        self.query_one("#run-title", Static).update(self._title())
        self.query_one("#run-error", Static).update("")
        self.query_one("#run-footer", Static).update(_STAGES[stage])
        if (
            stage == "review"
        ):  # Enter submits, whatever was highlighted when the human stepped back
            self.query_one("#run-actions", OptionList).highlighted = 0
        self.query_one(_SHOWN[stage][-1], Widget).focus()

    def action_back(self) -> None:
        parent = _PARENT.get(self._stage)
        if parent is None:
            self.dismiss(None)
        else:
            self._show_stage(parent)

    def _show_error(self, text: str) -> None:
        self.query_one("#run-error", Static).update(text)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if event.option_list.id == "run-worktrees":
            self._worktree = self._choices[index].path
            self._show_stage("flow")
        elif event.option_list.id == "run-flows":
            self._choose_flow(self._flows[index].name)
        elif event.option_list.id == "run-form":
            self._form_row_selected(index)
        elif event.option_list.id == "run-choices":
            self._keep_choice(index)
        else:
            self._apply_action(SUBMIT if index == 0 else COPY)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the editor keeps the typed value (Esc discards it)."""
        event.stop()
        row, text = self._changing, event.value.strip()
        fields = self._fields()
        if row < len(fields):
            self._values[fields[row].dest] = text
        else:
            self._extra = text
        self._show_stage("form")

    # --- the form -----------------------------------------------------------

    def _fields(self) -> tuple[Field, ...]:
        return self._form.fields if self._form else ()

    def _row_label(self, row: int) -> str:
        fields = self._fields()
        return fields[row].label if row < len(fields) else EXTRA_LABEL

    def _choose_flow(self, flow: str) -> None:
        """Show *flow*'s form: a fresh one for another worktree or Flow or after a failed build."""
        assert self._worktree is not None
        self._flow = flow
        pair = (self._worktree, flow)
        if pair != self._form_for or self._form_error:
            self._start_form(pair)
        self._show_stage("form")

    def _start_form(self, pair: tuple[Path, str]) -> None:
        """Forget the old form and build the one for *pair* in a worker."""
        _, flow = pair
        self._form_for = pair
        self._form = None
        self._form_error = ""
        self._building = True
        self._values = {}
        self._extra = ""
        self._changing = 0
        # The old Flow's rows must not stay selectable while the new form builds.
        self.query_one("#run-form", OptionList).clear_options()
        self._form_worker = self.run_worker(
            lambda: self._launcher.form(flow),
            thread=True,
            exclusive=True,
            group=FORM_GROUP,
            exit_on_error=False,
        )

    def _show_form(self) -> None:
        """While the form builds only the note shows, so no highlight moves under the cursor."""
        note = BUILDING if self._building else self._form_error
        if note and not self._building:
            note = f"The form cannot be built: {note}"
        self.query_one("#run-note", Static).update(note)
        self.query_one("#run-note", Static).display = bool(note)
        rows = self.query_one("#run-form", OptionList)
        rows.display = not self._building
        if not self._building:
            self._fill_rows()

    def _fill_rows(self) -> None:
        """One row per field, then the extra arguments and Review; the highlight is the last row used."""
        fields = self._fields()
        width = max([len(field.label) for field in fields] + [len(EXTRA_LABEL)])
        prompts = [
            f"{field.label:<{width}}  {field.shown(self._values.get(field.dest, ''))}"
            for field in fields
        ]
        prompts += [f"{EXTRA_LABEL:<{width}}  {self._extra}", REVIEW]
        rows = self.query_one("#run-form", OptionList)
        rows.clear_options()
        rows.add_options([Option(prompt) for prompt in prompts])
        rows.highlighted = min(self._changing, len(prompts) - 1)

    def _form_row_selected(self, row: int) -> None:
        if self._building:  # no row exists until the build lands
            return
        fields = self._fields()
        self._changing = row
        if row == len(fields) + 1:
            self._run_check()
        elif row == len(fields):
            self._edit(self._extra, (self._form or NO_FORM).extra_hint())
        else:
            field = fields[row]
            value = self._values.get(field.dest, "")
            if field.kind == "toggle":
                # A check still running was for the old value: it must not open review.
                self._values[field.dest] = "" if value else "on"
                self._checking = None
                self._show_error("")
                self._fill_rows()
            elif field.kind == "text":
                self._edit(value, field.hint)
            else:
                self._choose(field, value)

    def _edit(self, value: str, hint: str) -> None:
        editor = self.query_one("#run-edit", Input)
        editor.value = value
        editor.cursor_position = len(value)
        self.query_one("#run-hint", Static).update(hint)
        self._show_stage("edit")

    def _choose(self, field: Field, value: str) -> None:
        """Open the choice list for a three-way or list field, the current value highlighted."""
        values = field.values()
        choices = self.query_one("#run-choices", OptionList)
        choices.clear_options()
        choices.add_options([Option(text) for text in (DEFAULT, *values)])
        choices.highlighted = values.index(value) + 1 if value else 0
        self.query_one("#run-hint", Static).update(field.hint)
        self._show_stage("choice")

    def _keep_choice(self, index: int) -> None:
        field = self._fields()[self._changing]
        self._values[field.dest] = field.values()[index - 1] if index else ""
        self._show_stage("form")

    def _run_check(self) -> None:
        """Build the command from the form and have the Flow's own parser check it."""
        assert self._worktree is not None
        try:
            argv = command_argv(self._form or NO_FORM, self._values, self._extra)
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

    def _form_built(self, event: Worker.StateChanged) -> None:
        """Apply a finished build for the form now wanted; a crash shows as the note."""
        if event.worker is not self._form_worker:
            return  # a build the human replaced after it had finished, before this message ran
        if event.state is WorkerState.ERROR:
            error = event.worker.error
            self._form_error = (
                str(error) if isinstance(error, FormError) else f"{type(error).__name__}: {error}"
            )
        elif event.state is WorkerState.SUCCESS:
            built = event.worker.result
            assert isinstance(built, RunForm)
            self._form = built
            self._form_error = ""
        else:
            return
        event.stop()
        self._building = False
        if self._stage == "form":
            self._show_stage("form")

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply a finished check unless the human moved on or changed the form meanwhile."""
        if event.worker.group == FORM_GROUP:
            self._form_built(event)
            return
        if event.worker.group != CHECK_GROUP:
            return
        if (
            event.state is WorkerState.ERROR
        ):  # the check itself crashed: say so, never hang on "Checking…"
            event.stop()
            if self._checking is not None:  # else the human moved on or changed the form
                self._checking = None
                self._show_error(f"Argument check failed: {event.worker.error}")
            return
        if event.state is not WorkerState.SUCCESS:
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
