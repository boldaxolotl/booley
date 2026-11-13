"""The Dashboard's Run wizard: worktree, Flow or Specialist, its form, then review and submit.

One modal screen shows one stage at a time. A Flow's form stage is generated from its
human parser (``run_form``): a row per option, changed in a choice list or
a text editor stage, or for the Target and test fields a picker over the worktree's
Targets and the chosen Target's Test Runs, plus one extra-arguments row. The ``reviewer``
Specialist's form is written by hand (``specialist_form``) and has no extra-arguments row. It dismisses with the new
Job's row key once submitted, or ``None`` when the human backs out of the first
stage.
"""

from __future__ import annotations

import logging
import shlex
from collections.abc import Callable, Mapping
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

from booley.core.booley_layout import BooleyLayoutError
from booley.harness.dashboard.launch import (
    NO_FACTS,
    ArgumentsError,
    CheckResult,
    Launcher,
    RunFacts,
    SpecialistListing,
    shell_command,
)
from booley.harness.dashboard.review import (
    Occupants,
    queue_line,
    refusal,
    timeout_line,
    warning_lines,
)
from booley.harness.dashboard.run_form import (
    AUTOMATIC,
    DEFAULT,
    NO_FORM,
    Field,
    FormError,
    RunForm,
    command_argv,
)
from booley.harness.dashboard.specialist_form import FORM_NOT_BUILT, ReviewerForm
from booley.harness.dashboard.targets import (
    RunChoices,
    TargetListing,
    TargetsReader,
    WorktreeChoice,
    first_line,
)
from booley.harness.worktree_list import WorktreeListError
from booley.runtime.job_slots import AdmissionOutlook

logger = logging.getLogger(__name__)

CHECK_GROUP = "run-check"
# Not "targets-": the app's own worker handler reacts to those.
FORM_GROUP = "run-form"
TARGETS_GROUP = "run-targets"
TESTS_GROUP = "run-tests"
SPECIALISTS_GROUP = "run-specialists"
LOADING_SPECIALISTS = "Loading Specialists…"
LOADING_TARGETS, LOADING_RUNS = "Loading Targets…", "Loading Test Runs…"
NO_TARGET_YET = "Choose a Target first; or type Test Runs in the extra arguments."
# A Flow that is not Target-aware but drives another Flow's Targets: the Mutation Flow runs one
# Simulation Target, so its Target picker offers what the Simulation Flow can drive.
TARGET_KIND_OF_FLOW = {"mutation": "sim"}
TYPE_TARGETS = "Type Targets in the extra arguments."
TYPE_RUNS = "Type Test Runs in the extra arguments."
# What Enter on the Target row says when a Target detail opened the wizard.
PRESET_TARGET = "The Target comes from the Target detail; Esc goes back to it."
NO_FITTING_TARGET = (
    "No Target here can be driven by this Flow; type Targets in the extra arguments."
)
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
    "target": "↑↓ move · ⏎ choose · esc back",
    "tests": "↑↓ move · ⏎ toggle · esc back",
    "edit": "type the value · ⏎ keep · esc cancel",
    "review": "↑↓ choose · ⏎ apply · esc back",
}
# Esc goes to the parent stage; choosing or editing a value and checking the command return to the form.
_PARENT = {
    "flow": "worktree",
    "form": "flow",
    "choice": "form",
    "target": "form",
    "tests": "form",
    "edit": "form",
    "review": "form",
}
# The stages that change one form row; their title names the row.
_ROW_STAGES = ("choice", "edit", "target", "tests")
# Which widgets each stage shows; the last one takes the keyboard.
_SHOWN: dict[str, tuple[str, ...]] = {
    "worktree": ("#run-worktrees",),
    "flow": ("#run-flows",),
    "form": ("#run-note", "#run-form"),
    "choice": ("#run-hint", "#run-choices"),
    "target": ("#run-hint", "#run-picks"),
    "tests": ("#run-hint", "#run-picks"),
    "edit": ("#run-hint", "#run-edit"),
    "review": ("#run-command", "#run-review", "#run-actions"),
}


@dataclass(frozen=True)
class RunPreset:
    """Where a Target detail opened the wizard: the worktree, the Flow and the Target are chosen.

    The wizard starts at the form; Esc there closes it, back to the detail.
    """

    worktree: Path
    flow: str
    target: str


@dataclass(frozen=True)
class _Request:
    """What the human asked to run; a check answers exactly one."""

    worktree: Path
    kind: str
    name: str
    argv: tuple[str, ...]


@dataclass(frozen=True)
class _Checked:
    """A finished check: the shell's rejection, or the run's facts with the reads made for review.

    ``outlook`` is ``None`` when the run takes no slot and a string when the queue cannot be
    read; ``uncommitted`` is a string when the changes cannot be counted.
    """

    request: _Request
    error: str
    facts: RunFacts | None = None
    outlook: AdmissionOutlook | str | None = None
    uncommitted: int | str = 0


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
    #run-review { height: auto; }
    #run-note, #run-hint { color: $text-muted; height: auto; }
    """

    def __init__(
        self,
        launcher: Launcher,
        targets: TargetsReader,
        choices: tuple[WorktreeChoice, ...],
        message: str,
        preselect: Callable[[tuple[WorktreeChoice, ...]], Path | None],
        occupants: Callable[[Path], Occupants],
        preset: RunPreset | None = None,
    ) -> None:
        super().__init__()
        self._preset = preset
        self._launcher = launcher
        self._targets = targets  # the app's reader: its lock serializes every catalog read
        self._flows = launcher.flows()
        # What each row of the Flow or Specialist step stands for: (kind, name), or None when disabled.
        self._entries: list[tuple[str, str] | None] = []
        self._specialists: SpecialistListing | None = None  # None while being read
        self._specialists_for: Path | None = None
        self._specialists_worker: Worker[SpecialistListing] | None = None
        self._choices = choices
        self._message = message
        self._preselect = preselect
        self._occupants = occupants
        self._stage = "worktree"
        self._worktree: Path | None = None
        self._kind, self._name = "flow", ""
        self._argv: tuple[str, ...] = ()
        self._checking: _Request | None = None
        self._checked: _Checked | None = None  # the check the review screen shows
        # The form belongs to one (worktree, kind, name); None while it is being built or if it failed.
        self._form_for: tuple[Path, str, str] | None = None
        self._form: RunForm | ReviewerForm | None = None
        # The one build whose result counts.
        self._form_worker: Worker[RunForm | ReviewerForm] | None = None
        self._building = False
        self._form_error = ""
        self._values: dict[str, str] = {}
        self._picked: dict[str, tuple[str, ...]] = {}  # the Test Runs chosen, in pick order
        # The worktree's Targets and the chosen Target's Test Runs; None while being read.
        self._listing: TargetListing | None = None
        self._runs: RunChoices | None = None
        self._pick_values: list[str] = []  # what each row of the open picker stands for
        self._extra = ""
        self._changing = 0  # the form row a choice list or editor is changing

    def compose(self) -> ComposeResult:
        with Vertical(id="run-box"):
            yield Static(id="run-title", markup=False)
            yield OptionList(id="run-worktrees", markup=False)
            yield OptionList(id="run-flows", markup=False)
            yield Static(id="run-note", markup=False)
            yield OptionList(id="run-form", markup=False)
            yield Static(id="run-hint", markup=False)
            yield OptionList(id="run-choices", markup=False)
            yield OptionList(id="run-picks", markup=False)
            yield Input(id="run-edit", select_on_focus=False)
            yield Static(id="run-command", markup=False)
            yield Static(id="run-review", markup=False)
            yield OptionList(SUBMIT, COPY, id="run-actions", markup=False)
            yield Static(id="run-error", markup=False)
            yield Static(id="run-footer", markup=False)

    def on_mount(self) -> None:
        self.set_worktrees(self._choices, self._message)
        self._fill_entries()
        if self._preset is None:
            self._show_stage("worktree")
        else:
            self._worktree = self._preset.worktree
            self._choose_entry("flow", self._preset.flow)

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
        command = f"booley {self._kind} {self._name}"
        row = f"{command}: {self._row_label(self._changing)}" if self._stage in _ROW_STAGES else ""
        titles = {
            "worktree": "Run a Booley Flow or Specialist: choose the worktree",
            "flow": "Run a Booley Flow or Specialist: choose what to run",
            "form": f"{command} in {self._worktree.name if self._worktree else ''}",
            "review": "Run a Booley Flow or Specialist: review the command",
        }
        return titles.get(self._stage, row)

    def _show_stage(self, stage: str) -> None:
        """Show *stage* only, give it the keyboard and clear the last stage's error."""
        self._stage = stage
        self._checking = None
        for widget_id in {i for ids in _SHOWN.values() for i in ids}:
            self.query_one(widget_id, Widget).display = widget_id in _SHOWN[stage]
        if stage == "form":
            self._show_form()
        elif stage in ("target", "tests"):
            self._fill_picks(None)
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
        if parent is None or (self._preset is not None and self._stage == "form"):
            self.dismiss(None)
        else:
            self._show_stage(parent)

    def _show_error(self, text: str) -> None:
        self.query_one("#run-error", Static).update(text)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if event.option_list.id == "run-worktrees":
            self._choose_worktree(self._choices[index].path)
        elif event.option_list.id == "run-flows":
            entry = self._entries[index]
            assert entry is not None  # a disabled row is never selected
            self._choose_entry(*entry)
        elif event.option_list.id == "run-form":
            self._form_row_selected(index)
        elif event.option_list.id == "run-choices":
            self._keep_choice(index)
        elif event.option_list.id == "run-picks":
            self._pick_selected(index)
        elif event.option_list.id == "run-actions":
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
        if isinstance(self._form, ReviewerForm):
            return self._form.fields(self._values)
        return self._form.fields if self._form else ()

    def _has_extra(self) -> bool:
        """Only a Flow's form takes extra arguments: a Specialist runs what its fields write."""
        return self._kind == "flow"

    def _row_label(self, row: int) -> str:
        fields = self._fields()
        return fields[row].label if row < len(fields) else EXTRA_LABEL

    # --- the Flow or Specialist step ---------------------------------------

    def _choose_worktree(self, worktree: Path) -> None:
        """Remember the worktree and list its Flows at once; its Specialists follow from a worker."""
        self._worktree = worktree
        listing = self._specialists
        # A listing that failed is read again, so a fixed booley.toml shows on re-entry.
        if worktree != self._specialists_for or (listing is not None and listing.problem):
            self._specialists_for, self._specialists = worktree, None
            self._specialists_worker = self.run_worker(
                lambda: self._launcher.specialists(worktree),
                thread=True,
                exclusive=True,
                group=SPECIALISTS_GROUP,
                exit_on_error=False,
            )
        self._fill_entries()
        self._show_stage("flow")

    def _fill_entries(self) -> None:
        """The step's rows: the Flows, then the Specialists (or why there are none), keeping the highlight."""
        rows = [
            (Option(f"{flow.name} — {flow.blurb}"), ("flow", flow.name)) for flow in self._flows
        ]
        listing = self._specialists
        if listing is None:
            rows.append((Option(LOADING_SPECIALISTS, disabled=True), None))
        elif listing.problem:
            rows.append(
                (Option(f"Specialists unavailable: {listing.problem}", disabled=True), None)
            )
        for choice in listing.choices if listing else ():
            text = f"{choice.name} — {choice.blurb}"
            if choice.selectable:
                rows.append((Option(text), ("specialist", choice.name)))
            else:
                rows.append((Option(f"{text} ({FORM_NOT_BUILT})", disabled=True), None))
        options = self.query_one("#run-flows", OptionList)
        kept = options.highlighted
        options.clear_options()
        options.add_options([option for option, _ in rows])
        self._entries = [entry for _, entry in rows]
        options.highlighted = kept or 0

    def _specialists_read(self, event: Worker.StateChanged) -> None:
        """Show the Specialists of the worktree now chosen; a worker that crashed becomes the reason row."""
        if event.worker is not self._specialists_worker:
            return  # a read for a worktree the human has since replaced
        if event.state is WorkerState.ERROR:
            error = event.worker.error
            self._specialists = SpecialistListing(
                (), f"{type(error).__name__}: {first_line(error)}"
            )
        elif event.state is WorkerState.SUCCESS:
            assert isinstance(event.worker.result, SpecialistListing)
            self._specialists = event.worker.result
        else:
            return
        event.stop()
        self._fill_entries()

    def _choose_entry(self, kind: str, name: str) -> None:
        """Show the form of *name*: a fresh one for another worktree or entry or after a failed build."""
        assert self._worktree is not None
        self._kind, self._name = kind, name
        triple = (self._worktree, kind, name)
        listing_failed = self._listing is not None and bool(self._listing.problem)
        if triple != self._form_for or self._form_error:
            self._start_form(triple)
        elif listing_failed:  # read the Targets again, keeping what was typed
            self._listing = None
            self._start_listing(triple[0])
        self._show_stage("form")

    def _start_form(self, triple: tuple[Path, str, str]) -> None:
        """Forget the old form and build the one for *triple* in a worker."""
        worktree, kind, name = triple
        self._form_for = triple
        self._form = None
        self._form_error = ""
        self._building = True
        self._values = {}
        self._picked = {}
        self._listing = self._runs = None
        self._extra = ""
        self._changing = 0
        # The old Flow's rows must not stay selectable while the new form builds.
        self.query_one("#run-form", OptionList).clear_options()
        if kind == "flow":  # only a Flow's form has the Target and test pickers
            self._start_listing(worktree)
        self._form_worker = self.run_worker(
            lambda: self._launcher.form(kind, name, worktree),
            thread=True,
            exclusive=True,
            group=FORM_GROUP,
            exit_on_error=False,
        )

    def _start_listing(self, worktree: Path) -> None:
        """Read *worktree*'s Targets in a worker."""
        self.run_worker(
            lambda: self._read_listing(worktree),
            thread=True,
            exclusive=True,
            group=TARGETS_GROUP,
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
        """One row per field, then the extra arguments (Flows) and Review; the highlight is the last row used."""
        fields = self._fields()
        labels = [field.label for field in fields] + ([EXTRA_LABEL] if self._has_extra() else [])
        width = max((len(label) for label in labels), default=0)
        prompts = [f"{field.label:<{width}}  {self._shown(field)}" for field in fields]
        if self._has_extra():
            prompts.append(f"{EXTRA_LABEL:<{width}}  {self._extra}")
        prompts.append(REVIEW)
        rows = self.query_one("#run-form", OptionList)
        rows.clear_options()
        rows.add_options([Option(prompt) for prompt in prompts])
        rows.highlighted = min(self._changing, len(prompts) - 1)

    def _shown(self, field: Field) -> str:
        """How the form row of *field* spells what is chosen for it."""
        if field.kind == "tests":
            return field.shown(self._picked.get(field.dest, ()))
        return field.shown(self._values.get(field.dest, ""))

    def _read_listing(self, worktree: Path) -> TargetListing:
        """The worktree's Targets; whatever the reader raises becomes the listing's problem."""
        try:
            return self._targets.listing(worktree)
        except Exception as exc:
            logger.exception("Run wizard Targets read failed")
            return TargetListing(worktree, (), f"{type(exc).__name__}: {first_line(exc)}")

    def _read_runs(self, worktree: Path, selector: str) -> RunChoices:
        """The Target's Test Runs; whatever the reader raises becomes the problem."""
        try:
            return self._targets.test_runs(worktree, selector)
        except Exception as exc:
            logger.exception("Run wizard Test Runs read failed")
            why = f"Tests unavailable: {type(exc).__name__}: {first_line(exc)}"
            return RunChoices(worktree, selector, (), why)

    def _form_row_selected(self, row: int) -> None:
        if self._building:  # no row exists until the build lands
            return
        fields = self._fields()
        self._changing = row
        if row == len(fields) + int(self._has_extra()):
            self._run_check()
        elif row == len(fields):
            self._edit(self._extra, (self._form or NO_FORM).extra_hint())
        else:
            field = fields[row]
            value = self._values.get(field.dest, "")
            if field.kind in ("toggle", "more"):
                # A check still running was for the old value: it must not open review.
                self._values[field.dest] = "" if value else "on"
                self._checking = None
                self._show_error("")
                self._fill_rows()
            elif field.kind == "text":
                self._edit(value, field.hint)
            elif field.kind == "target":
                if self._preset is None:
                    self._show_stage("target")
                else:
                    self._show_error(PRESET_TARGET)
            elif field.kind == "tests":
                self._open_tests()
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
        values, skipped = field.values(), self._default_rows(field)
        choices = self.query_one("#run-choices", OptionList)
        choices.clear_options()
        choices.add_options([Option(text) for text in (*(DEFAULT,) * skipped, *values)])
        choices.highlighted = values.index(value) + skipped if value else 0
        self.query_one("#run-hint", Static).update(field.hint)
        self._show_stage("choice")

    # --- the Target and test pickers -----------------------------------------

    def _target_value(self) -> str:
        """The Target chosen on the form (``""`` is automatic)."""
        return next(
            (self._values.get(f.dest, "") for f in self._fields() if f.kind == "target"), ""
        )

    def _open_tests(self) -> None:
        """Open the Test Run picker, reading the chosen Target's Test Runs unless they are at hand."""
        assert self._form_for is not None
        worktree, target = self._form_for[0], self._target_value()
        # A failed read is tried again: the human may have fixed what it named.
        known = (
            self._runs is not None
            and not self._runs.problem
            and (self._runs.worktree, self._runs.selector) == (worktree, target)
        )
        if target and not known:
            self._runs = None
            self.run_worker(
                lambda: self._read_runs(worktree, target),
                thread=True,
                exclusive=True,
                group=TESTS_GROUP,
                exit_on_error=False,
            )
        self._show_stage("tests")

    def _picker(self, field: Field) -> tuple[str, list[tuple[str, str]]]:
        """The hint and the (row text, value) rows of the open picker for *field*."""
        return self._target_picker(field) if field.kind == "target" else self._tests_picker(field)

    def _target_picker(self, field: Field) -> tuple[str, list[tuple[str, str]]]:
        rows = [] if field.required else [(AUTOMATIC, "")]
        listing = self._listing
        if listing is None:
            return LOADING_TARGETS, rows
        if listing.problem:
            return f"Targets unavailable: {listing.problem}. {TYPE_TARGETS}", rows
        drives = TARGET_KIND_OF_FLOW.get(self._name, self._name)
        fitting = [row.selector for row in listing.rows if drives in row.flows]
        rows += [(selector, selector) for selector in fitting]
        return (field.hint if fitting else NO_FITTING_TARGET), rows

    def _tests_picker(self, field: Field) -> tuple[str, list[tuple[str, str]]]:
        runs = self._runs
        if not self._target_value():
            return NO_TARGET_YET, []
        if runs is None:
            return LOADING_RUNS, []
        if runs.problem:
            return f"{runs.problem}. {TYPE_RUNS}", []
        chosen = self._picked.get(field.dest, ())
        rows = [(f"[{'x' if run in chosen else ' '}] {run}", run) for run in runs.runs]
        return field.hint, rows

    def _fill_picks(self, highlight: int | None) -> None:
        """Fill the open picker; *highlight* is the row to keep, else the current Target's."""
        field = self._fields()[self._changing]
        hint, rows = self._picker(field)
        self._pick_values = [value for _, value in rows]
        picks = self.query_one("#run-picks", OptionList)
        picks.clear_options()
        picks.add_options([Option(text) for text, _ in rows])
        if highlight is None:
            current = self._values.get(field.dest, "")
            highlight = self._pick_values.index(current) if current in self._pick_values else 0
        if rows:
            picks.highlighted = min(highlight, len(rows) - 1)
        self.query_one("#run-hint", Static).update(hint)

    def _pick_selected(self, index: int) -> None:
        """Enter in a picker: a Target is chosen and closes it; a Test Run toggles in place."""
        field = self._fields()[self._changing]
        value = self._pick_values[index]
        if field.kind == "tests":
            chosen = self._picked.get(field.dest, ())
            self._picked[field.dest] = (
                tuple(run for run in chosen if run != value)
                if value in chosen
                else (*chosen, value)
            )
            self._fill_picks(index)
            return
        if value != self._values.get(field.dest, ""):
            self._values[field.dest] = value
            self._picked = {}  # the Test Runs belong to the Target they were listed for
            self._runs = None
        self._show_stage("form")

    @staticmethod
    def _default_rows(field: Field) -> int:
        """1 when the choice list opens with a ``default`` row; a required field has none."""
        return 0 if field.required else 1

    def _keep_choice(self, index: int) -> None:
        field = self._fields()[self._changing]
        position = index - self._default_rows(field)
        self._values[field.dest] = field.values()[position] if position >= 0 else ""
        if isinstance(self._form, ReviewerForm):
            self._values = self._form.changed(self._values)
        self._show_stage("form")

    def _form_argv(self) -> tuple[str, ...] | None:
        """The command's words from the form, or ``None`` after showing why they cannot be built."""
        form = self._form
        try:
            if isinstance(form, ReviewerForm):
                problem = form.refusal(self._values)
                if problem:
                    raise ArgumentsError(problem)
                return form.argv(self._values)
            return command_argv(form or NO_FORM, self._all_values(), self._extra)
        except ArgumentsError as exc:
            self._show_error(str(exc))
            return None

    def _run_check(self) -> None:
        """Build the command from the form and have the endpoint's own parser check it."""
        assert self._worktree is not None
        argv = self._form_argv()
        if argv is None:
            return
        request = _Request(self._worktree, self._kind, self._name, argv)
        self._checking = request
        self._show_error("Checking…")
        self.run_worker(
            lambda: self._check(request),
            thread=True,
            exclusive=True,
            group=CHECK_GROUP,
            exit_on_error=False,
        )

    def _all_values(self) -> Mapping[str, str | tuple[str, ...]]:
        """Every field's chosen value: text for most, a tuple of Test Runs for the test field."""
        return {**self._values, **self._picked}

    def _read_applies(self, result: TargetListing | RunChoices) -> bool:
        """Whether *result* answers the request the form wants now (a replaced one is dropped)."""
        if self._form_for is None or result.worktree != self._form_for[0]:
            return False
        return isinstance(result, TargetListing) or result.selector == self._target_value()

    def _read_finished(self, event: Worker.StateChanged) -> None:
        """Keep a finished listing or Test Run read the form still wants; refill its open picker."""
        if event.state is not WorkerState.SUCCESS:
            return  # the reads guard themselves, so only success carries a result
        event.stop()
        result = event.worker.result
        assert isinstance(result, (TargetListing, RunChoices))
        if not self._read_applies(result):
            return
        if isinstance(result, TargetListing):
            self._listing = result
        else:
            self._runs = result
        if self._stage == ("target" if isinstance(result, TargetListing) else "tests"):
            self._fill_picks(self.query_one("#run-picks", OptionList).highlighted)

    def _check(self, request: _Request) -> _Checked:
        """The argument check, then (when it passes) the reads the review screen shows.

        Runs in a worker thread: it spawns a child and reads the slot store and Git.
        """
        result: CheckResult = self._launcher.check(
            request.worktree, request.kind, request.name, request.argv
        )
        if result.facts is None:
            return _Checked(request, result.error or NO_FACTS)
        try:
            uncommitted: int | str = self._launcher.uncommitted(request.worktree)
        except (WorktreeListError, BooleyLayoutError) as exc:
            uncommitted = str(exc)
        return _Checked(request, "", result.facts, self._read_outlook(result.facts), uncommitted)

    def _read_outlook(self, facts: RunFacts) -> AdmissionOutlook | str | None:
        """The queue a run would meet; ``None`` when it takes no slot, a reason when unreadable."""
        if not facts.takes_slot or facts.job_class is None:
            return None
        try:
            return self._launcher.outlook(facts.job_class)
        except OSError as exc:
            return str(exc) or type(exc).__name__

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
            assert isinstance(built, (RunForm, ReviewerForm))
            self._form = built
            if isinstance(built, ReviewerForm):
                self._values = built.initial()
            self._form_error = ""
        else:
            return
        event.stop()
        self._building = False
        self._apply_preset()
        if self._stage == "form":
            self._show_stage("form")

    def _apply_preset(self) -> None:
        """Fill in the Target a Target detail chose.

        Without a built form (or without a Target field to hold it) the Target goes to the
        extra arguments instead, so the command still names it.
        """
        if self._preset is None:
            return
        dest = next((f.dest for f in self._fields() if f.kind == "target"), None)
        if dest is None:
            self._extra = shlex.join(["--target", self._preset.target])
        else:
            self._values[dest] = self._preset.target

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply a finished check unless the human moved on or changed the form meanwhile."""
        group = event.worker.group
        handlers = {
            SPECIALISTS_GROUP: self._specialists_read,
            FORM_GROUP: self._form_built,
            TARGETS_GROUP: self._read_finished,
            TESTS_GROUP: self._read_finished,
        }
        if group in handlers:
            handlers[group](event)
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
        self._checked = checked
        self._show_stage("review")
        self.query_one("#run-command", Static).update(
            shell_command(
                checked.request.worktree, checked.request.kind, checked.request.name, self._argv
            )
        )
        self._render_review(checked.outlook)

    def _render_review(self, outlook: AdmissionOutlook | str | None) -> None:
        """Show the queue verdict, the timeout and the warnings; a full queue also says why in the error line."""
        checked = self._checked
        assert checked is not None and checked.facts is not None
        occupants = self._occupants(checked.request.worktree)
        lines = [queue_line(checked.facts, outlook), timeout_line(checked.facts)]
        lines += warning_lines(occupants, checked.uncommitted)
        self.query_one("#run-review", Static).update("\n".join(line for line in lines if line))
        refusal_text = (
            refusal(outlook) if isinstance(outlook, AdmissionOutlook) and outlook.full else ""
        )
        self._show_error(refusal_text)

    def _apply_action(self, action: str) -> None:
        assert self._worktree is not None
        command = shell_command(self._worktree, self._kind, self._name, self._argv)
        if action == COPY:
            self.app.copy_to_clipboard(command)
            self.notify(COPY_NOTICE)
            return
        checked = self._checked
        if checked is not None and checked.facts is not None and checked.facts.takes_slot:
            # The queue may have changed since the screen opened: read it again.
            outlook = self._read_outlook(checked.facts)
            self._render_review(outlook)
            if isinstance(outlook, AdmissionOutlook) and outlook.full:
                return
        try:
            key = self._launcher.submit(self._worktree, self._kind, self._name, self._argv)
        except OSError as exc:
            self._show_error(f"Could not start the Job: {exc}")
            return
        self.dismiss(key)
