"""The Measurement Report as Markdown and as terminal text (ADR 0092, #3045, #3748).

Contract for the caller (``booley measure``, #3507): build the view with
:func:`booley.measurement.run_view.run_view`, print :func:`terminal_report` and publish
:func:`write_markdown_report`. Both render from the view JSON alone (no store,
repository or clock) out of one content model, so they hold the same facts in the
same order.

Decisions: the Target column and the rerun command use the Target key as written in
``metrics.toml`` (``vlnv#name`` only for Targets with no metrics); a ``tests_total``
drop that is also a worse metric is shown once, under "``tests_total`` dropped" (matched
on the rules entry's metric); the 20-line cap counts file and submodule lines together,
while protected and constraint edits get their own uncapped lines; red and amber are
words in Markdown, never colour (the report opens with the state in words: Red, Amber
with the header's reasons, Green; a rules change names its class, "loosened" and so on,
in its text); integers print whole and floats with ``:.12g`` (Δ signed) so float noise
never reaches the report; in Markdown, specials in user text are backslash-escaped. An unmet
lint or mutation bound lists the first findings or survived Mutants behind it after the failing tests.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from booley.measurement.run_document import is_measured
from booley.presentation.colors import bold
from booley.presentation.report_blocks import (
    Block,
    Code,
    Rows,
    Section,
    Text,
    Tone,
    markdown_sections,
    paint,
    terminal_sections,
)
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import format_duration

_LIST_CAP = 20
_MISSING = "—"
_Address = tuple[str, str]  # (identity, flow)


def _num(value: float | None, *, signed: bool = False) -> str:
    """Integers whole; floats to 12 significant digits, which drops float noise."""
    if value is None:
        return _MISSING
    sign = "+" if signed else ""
    return f"{value:{sign}d}" if isinstance(value, int) else format(value, f"{sign}.12g")


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


class _Names:
    """Document lookups shared by the section builders."""

    def __init__(self, document: Mapping[str, Any]) -> None:
        self.target: dict[_Address, str] = {}
        self.entry: dict[tuple[str, str, str], Mapping[str, Any]] = {}
        for rules in document["rules"]:
            address = (rules["identity"], rules["flow"])
            self.target[address] = rules["target"]
            for entry in (*rules["bounds"], *rules["tracked"]):
                self.entry[(*address, entry["key"])] = entry

    def label(self, identity: str, flow: str) -> str:
        """The Target key as written, then the Flow."""
        return f"{self.target[(identity, flow)]} {flow}"

    def bound(self, identity: str, flow: str, key: str) -> str:
        entry = self.entry[(identity, flow, key)]
        if "limit" not in entry:
            return "tracked"
        return f"{'≤' if entry['limit'] == 'max' else '≥'} {_num(entry['value'])}"


def _tone(document: Mapping[str, Any]) -> Tone:
    """Fail for an unmet or error result; green only for a Measured run with nothing amber."""
    counts = document["view"]["counts"]
    if counts["unmet"] or counts["error"]:
        return "fail"
    stored = document["view"]["target"] is None and is_measured(document)
    return "warn" if document["view"]["amber"] or not stored else "met"


_STATE_WORDS: dict[Tone, str] = {
    "fail": "Red: not accepted",
    "warn": "Amber: needs review",
    "met": "Green: accepted",
}


def _state_line(document: Mapping[str, Any]) -> str:
    """The state the terminal shows as colour, in words; amber adds the header's reasons."""
    tone = _tone(document)
    reasons = f" ({', '.join(_states(document))})" if tone == "warn" else ""
    return _STATE_WORDS[tone] + reasons


def _verdict_line(document: Mapping[str, Any]) -> Text:
    counts = document["view"]["counts"]
    parts = [
        f"{counts['unmet']} unmet" if counts["unmet"] else "",
        _count(counts["enforced_error"], "error") if counts["enforced_error"] else "",
        (
            f"{counts['enforced_not_evaluated']} not evaluated"
            if counts["enforced_not_evaluated"]
            else ""
        ),
    ]
    enforced = counts["enforced"]
    failing = ", ".join(part for part in parts if part)
    text = (
        f"{failing} of {enforced} enforced"
        if failing
        else f"{enforced} of {enforced} enforced met"
    )
    if counts["tracked_error"]:
        text += f"; {_count(counts['tracked_error'], 'tracked error')}"
    if counts["tracked_not_evaluated"]:
        text += f"; {counts['tracked_not_evaluated']} tracked not evaluated"
    return Text(text, _tone(document))


def _base_lines(view: Mapping[str, Any]) -> list[Block]:
    base, rules_base, base_run = view["base"], view["rules_base"], view["base_run"]
    lines: list[Block]
    if base["commit"] is None:
        lines = [Text(f"no base: {base['no_base']}")]
    else:
        asked = base["requested"]
        extra = f", requested `{asked}`" if asked not in (None, base["commit"]) else ""
        lines = [Text(f"Base `{base['commit']}` ({base['choice']}){extra}")]
    if rules_base is not None and rules_base["commit"] is None:
        lines.append(Text(f"rules: no base: {rules_base['no_base']}"))
    elif rules_base is not None:
        lines.append(Text(f"rules Base `{rules_base['commit']}` ({rules_base['choice']})"))
    if base_run["problem"] is not None:
        lines.append(Text(f"base run unreadable: {base_run['problem']}", "warn"))
    elif base_run["base_run_id"] is not None:
        differs = " (its rules differ)" if base_run["rules_differ"] else ""
        lines.append(Text(f"base run `{base_run['base_run_id']}`{differs}"))
    elif base_run["command"] is not None:
        lines.append(Text(f"no base run; create it with `{base_run['command']}`"))
    return lines


def _run_lines(document: Mapping[str, Any]) -> list[Block]:
    """Toolchain, history and what a stopped or cancelled run left undone."""
    toolchain, history = document["toolchain"], document["view"]["history"]
    parts = [] if toolchain["image"] is None else [f"image {toolchain['image']}"]
    parts.append(f"Booley {toolchain['booley']} ({toolchain['booley_kind']})")
    parts.extend([toolchain["booley_note"]] if toolchain["booley_note"] else [])
    parts.extend(f"{tool} {version}" for tool, version in toolchain["tools"].items())
    lines: list[Block] = [Text(f"Toolchain: {'; '.join(parts)}")]
    if history is None:
        target = document["view"]["target"]
        lines.append(
            Text(
                f"`--target` `{target}`: only its tables; "
                "not stored, never makes the commit Measured"
            )
        )
    else:
        place = f"run {history['position']} of {history['total']} for this tree"
        if history["earlier"]:
            place += "; earlier: " + ", ".join(r["outcome"] for r in history["earlier"])
        lines.append(Text(place))
        lines.extend(Text(f"history: {problem}", "warn") for problem in history["problems"])
    if document["status"] == "stopped":
        left = document["view"]["counts"]["not_evaluated"]
        lines.append(Text(f"stopped early: {left} not evaluated, use `--all`", "warn"))
    elif document["status"] == "cancelled":
        lines.append(Text("cancelled", "warn"))
    return lines


def _states(document: Mapping[str, Any]) -> list[str]:
    view = document["view"]
    states = [] if document["status"] == "completed" else [document["status"]]
    states.extend(["failing"] if _tone(document) == "fail" else [])
    states.extend(view["amber"])
    if view["target"] is not None:
        measured = "not stored"
    else:
        measured = "measured" if is_measured(document) else "not measured"
    return states or [measured]


def _header(document: Mapping[str, Any]) -> Section:
    view = document["view"]
    commit, tone = view["commit"], _tone(document)
    blocks: list[Block] = [
        Text(f"Run {document['run_id']}: {', '.join(_states(document))}", tone),
        Text(f"Commit `{commit['sha']}`, tree `{commit['tree']}`"),
    ]
    if commit["measured_as"] is not None:
        blocks.append(Text(f"measured as `{commit['measured_as']}`, identical tree"))
    if commit["booley_tree"] is not None:
        suffix = f" (commit `{commit['booley_commit']}`)" if commit["booley_commit"] else ""
        blocks.append(Text(f"Booley Directory tree `{commit['booley_tree']}`{suffix}"))
    blocks.extend(_base_lines(view))
    blocks.extend(_run_lines(document))
    blocks.append(Text(f"Commit message: {commit['subject']}"))
    return ("Run", [*blocks, *([Code(commit["body"])] if commit["body"] else [])])


def _failing(document: Mapping[str, Any], names: _Names) -> Section:
    results = {
        (table["identity"], table["flow"], result["key"]): result
        for table in document["tables"]
        for result in table["results"]
    }
    blocks: list[Block] = []
    for item in document["view"]["excerpts"]:
        address, key = (item["identity"], item["flow"]), item["key"]
        value = _num(results[(*address, key)]["value"])
        bound = names.bound(*address, key)
        blocks.append(
            Text(f"{names.label(*address)} {key}: {value} vs {bound}, {item['verdict']}", "fail")
        )
        blocks.append(Code(item["excerpt"] or "(no detail)"))
        blocks.append(Code(f"booley measure --target {names.target[address]}\nbooley measure"))
    return ("Failing", blocks)


def _metrics(document: Mapping[str, Any], names: _Names) -> Section:
    deltas = {
        (item["identity"], item["flow"], item["key"]): item
        for item in document["view"]["base_run"]["deltas"]
    }
    order = list(
        dict.fromkeys(names.target[(t["identity"], t["flow"])] for t in document["tables"])
    )
    tables = sorted(
        document["tables"], key=lambda t: order.index(names.target[(t["identity"], t["flow"])])
    )
    verdict_tones: dict[str, Tone] = {"met": "met", "unmet": "fail", "error": "fail"}
    rows: list[tuple[str, ...]] = []
    tones: list[Tone] = []
    for table in tables:
        for result in table["results"]:
            key = (table["identity"], table["flow"], result["key"])
            entry, delta = names.entry[key], deltas[key]
            shown = (
                str(delta["missing"])
                if delta["delta"] is None
                else _num(delta["delta"], signed=True)
            )
            verdict = result["verdict"]
            cells = (
                names.target[key[:2]],
                table["flow"],
                entry["test_run"] or entry["prefix"] or "",
                entry["metric"],
                _num(result["value"]),
                names.bound(*key),
                shown,
                verdict.replace("_", " "),
            )
            rows.append(cells)
            tones.append(verdict_tones.get(verdict))
    headers = ("Target", "Flow", "Clock/Test", "Metric", "Value", "Bound", "Δ", "Verdict")
    return ("Metrics", [Rows(headers, tuple(rows), tuple(tones))])


def _times(document: Mapping[str, Any], names: _Names) -> Section:
    lines: list[Block] = []
    for table in document["tables"]:
        seconds = table["wall_time_s"]
        shown = "not started" if seconds is None else format_duration(seconds)
        lines.append(Text(f"{names.label(table['identity'], table['flow'])}: {shown}"))
    return ("Wall time", [*lines, Text(f"total: {format_duration(document['wall_time_s'])}")])


def _change_line(change: Mapping[str, Any]) -> Text:
    old, new, kind = change["old"], change["new"], f"{change['kind']} ({change['direction']})"
    if old is not None and new is not None:
        detail = kind if old == new else f"{kind}, {_num(old)} → {_num(new)}"
    else:  # a bound or table added or removed, or a tracked entry edited
        detail = ", ".join([kind, *([f"new {_num(new)}"] if new is not None else [])])
        detail += f", was {_num(old)}" if old is not None else ""
    where = " ".join(part for part in (change["target"], change["flow"], change["key"]) if part)
    note = f" — {change['note']}" if change["note"] else ""
    return Text(f"{where}: {detail}{note}", "warn" if change["direction"] == "loosened" else None)


def _rules_diff(view: Mapping[str, Any]) -> Section:
    diff = view["rules_diff"]
    ordered = sorted(diff["changes"], key=lambda change: change["direction"] != "loosened")
    blocks: list[Block] = [_change_line(change) for change in ordered]
    if diff["not_compared"] is not None:
        blocks.append(Text(f"rules not compared: {diff['not_compared']}"))
    if diff["unreadable"] is not None:
        blocks.append(Text(f"rules file unreadable: {diff['unreadable']}", "warn"))
    if diff["tests_diff"]:
        blocks.extend([Text("tests changed", "warn"), Code(diff["tests_diff"])])
    return ("Rules changes", blocks or [Text("no rules changes")])


def _warnings(document: Mapping[str, Any], names: _Names) -> Section:
    view = document["view"]
    base_run = view["base_run"]
    blocks: list[Block] = [Text(reason, "warn") for reason in view["amber"]]
    dropped = {(item["identity"], item["flow"]) for item in base_run["tests_dropped"]}
    for item in base_run["tests_dropped"]:
        label = names.label(item["identity"], item["flow"])
        move = f"{_num(item['base_total'])} → {_num(item['total'])}"
        blocks.append(Text(f"`tests_total` dropped: {label} {move}", "warn"))
    if base_run["toolchain_changes"]:
        changed = ", ".join(base_run["toolchain_changes"])
        blocks.append(Text(f"toolchain differs from the base run: {changed}", "warn"))
    for item in base_run["worsened"]:
        address = (item["identity"], item["flow"])
        if names.entry[(*address, item["key"])]["metric"] == "tests_total" and address in dropped:
            continue
        move = f"{_num(item['base_value'])} → {_num(item['value'])}"
        blocks.append(
            Text(f"worse than base: {names.label(*address)} {item['key']} {move}", "warn")
        )
    return ("Warnings", blocks)


def _submodule_line(change: Mapping[str, Any]) -> str:
    old, new = change["old"], change["new"]
    if old is None:
        move = f"added {new[:7]}"
    elif new is None:
        move = f"removed {old[:7]}"
    else:
        move = f"{old[:7]}..{new[:7]}"
    return f"submodule {change['path']} {move}" + (" (protected)" if change["protected"] else "")


def _counts_line(changed: Mapping[str, Any]) -> str:
    words = {"A": "added", "M": "modified", "D": "deleted", "T": "type changed"}
    seen = [file["status"] for file in changed["files"]]
    parts = [f"{seen.count(code)} {word}" for code, word in words.items() if code in seen]
    detail = f" ({', '.join(parts)})" if parts else ""
    return (
        f"{_count(changed['file_count'], 'file')} changed{detail}, "
        f"{_count(changed['submodule_count'], 'submodule')}"
    )


def _changed(view: Mapping[str, Any]) -> Section:
    changed = view["changed"]
    files, listed = ([], []) if changed is None else (changed["files"], [])
    blocks: list[Block] = [Text(f"no base: {view['base']['no_base']}")]
    if changed is not None:
        listed = [
            f"{f['status']} {f['path']}" + (" (protected)" if f["protected"] else "")
            for f in files
        ]
        listed.extend(_submodule_line(change) for change in changed["submodules"])
        blocks = [Text(_counts_line(changed)), *(Text(line) for line in listed[:_LIST_CAP])]
        if len(listed) > _LIST_CAP:
            blocks.append(Text(f"... and {len(listed) - _LIST_CAP} more"))
    # Edits are never capped, so the list cannot hide them.
    edits = view["protected_edits"]
    for label, paths in (
        ("protected edits", edits["repository"]),
        ("constraint edits", [f["path"] for f in files if f["constraint"]]),
        ("protected edits in the Booley Directory", edits["booley_directory"]),
    ):
        blocks.extend([Text(f"{label}: {', '.join(paths)}", "warn")] if paths else [])
    return ("Changed files", blocks)


def _failing_tests(document: Mapping[str, Any], names: _Names) -> Section:
    blocks: list[Block] = []
    for table in document["tables"]:
        values = table["values"] or {"plain": {}}
        if values["plain"].get("tests_failed", 0) <= 0:
            continue
        failed, total = (
            _num(values["plain"]["tests_failed"]),
            _num(values["plain"].get("tests_total")),
        )
        label = names.label(table["identity"], table["flow"])
        blocks.append(Text(f"{label}: {failed} failed of {total} tests", "fail"))
        listed = values.get("counted_items", {}).get("tests_failed", [])  # absent in older runs
        blocks.extend(_listed(listed, values["plain"]["tests_failed"]))
        blocks.extend(_flow_report_lines(document, table, label) if listed else [])
    return ("Failing tests", blocks)


def _listed(lines: list[str], count: int) -> list[Block]:
    """The *lines* behind *count*, ``... and N more`` when they are fewer."""
    if not lines:
        return []
    more = count - len(lines)
    return [Code("\n".join([*lines, *([f"... and {more} more"] if more > 0 else [])]))]


# Per Flow, the bound metric whose unmet result lists items: (the metric whose items are
# listed, the noun counted). A mutation score below its bound comes from the survived Mutants.
_UNMET_ITEMS: dict[str, dict[str, tuple[str, str]]] = {
    "lint": {"errors": ("errors", "error"), "warnings": ("warnings", "warning")},
    "mutation": {
        "mutants_survived": ("mutants_survived", "survived Mutant"),
        "mutation_score": ("mutants_survived", "survived Mutant"),
    },
}


def _unmet_items(document: Mapping[str, Any], names: _Names, flow: str, title: str) -> Section:
    """What an unmet *flow* bound counts: the stored items of each such table, then its report."""
    blocks: list[Block] = []
    for table in document["tables"]:
        if table["flow"] != flow:
            continue
        values = table["values"] or {"plain": {}}
        label = names.label(table["identity"], flow)
        unmet = (
            _UNMET_ITEMS[flow].get(names.entry[(table["identity"], flow, result["key"])]["metric"])
            for result in table["results"]
            if result["verdict"] == "unmet"
        )
        listed: list[Block] = []
        for metric, noun in dict.fromkeys(item for item in unmet if item):
            lines = values.get("counted_items", {}).get(metric, [])  # absent in older runs
            count = values["plain"].get(metric, 0)
            listed.extend([Text(f"{label}: {_count(count, noun)}", "fail")] if lines else [])
            listed.extend(_listed(lines, count))
        blocks.extend([*listed, *(_flow_report_lines(document, table, label) if listed else [])])
    return (title, blocks)


def _flow_report_lines(
    document: Mapping[str, Any], table: Mapping[str, Any], label: str
) -> list[Block]:
    """The path of *table*'s kept ``report.json``, where the failures not listed are."""
    return [
        Text(f"{label} report: `{path}`")
        for item in document["view"]["evidence"]
        if (item["identity"], item["flow"]) == (table["identity"], table["flow"])
        for path in item["reports"]
        if Path(path).name == "report.json"
    ]


def _footer(document: Mapping[str, Any], names: _Names) -> Section:
    blocks: list[Block] = []
    for item in document["view"]["evidence"]:
        label = names.label(item["identity"], item["flow"])
        blocks.extend(Text(f"{label} report: `{path}`") for path in item["reports"])
        blocks.extend(
            Text(f"{label}: {report['name']} ({report['bytes']} bytes, not kept)")
            for report in item["dropped"]
        )
    paths = document["view"]["paths"]
    return (
        "Files",
        [*blocks, Text(f"Markdown: `{paths['markdown']}`"), Text(f"JSON: `{paths['json']}`")],
    )


def _sections(document: Mapping[str, Any]) -> list[Section]:
    """Every section in the report's order; one with nothing to say is left out."""
    names, view = _Names(document), document["view"]
    untabled = view["targets_without_table"]
    targets = [Text(f"Targets with no metrics: {', '.join(untabled)}")] if untabled else []
    sections = [
        _header(document),
        _failing(document, names),
        _metrics(document, names),
        _times(document, names),
        _rules_diff(view),
        _warnings(document, names),
        ("Targets", targets),
        _changed(view),
        _failing_tests(document, names),
        _unmet_items(document, names, "lint", "Lint findings"),
        _unmet_items(document, names, "mutation", "Survived Mutants"),
        _footer(document, names),
    ]
    return [section for section in sections if section[1]]


def markdown_report(document: Mapping[str, Any]) -> str:
    """The Markdown Measurement Report of a view JSON (``run_view`` output)."""
    parts = [
        "# Measurement Report",
        f"**{_state_line(document)}**",
        f"**{_verdict_line(document).text}**",
    ]
    parts.extend(markdown_sections(_sections(document)))
    return "\n\n".join(parts) + "\n"


def write_markdown_report(document: Mapping[str, Any]) -> Path:
    """Publish the Markdown report atomically at the view's ``paths.markdown``; last write wins."""
    path = Path(document["view"]["paths"]["markdown"])
    atomic_replace_bytes(path, markdown_report(document).encode("utf-8"), mode=0o644)
    return path


def terminal_report(document: Mapping[str, Any], *, width: int | None = None) -> str:
    """The terminal Measurement Report of a view JSON; plain text when colour is off."""
    verdict = _verdict_line(document)
    lines = [bold("Measurement Report"), paint(verdict.tone)(verdict.text)]
    lines.extend(terminal_sections(_sections(document), width))
    return "\n".join(lines) + "\n"


def agent_summary(document: Mapping[str, Any]) -> str:
    """The Developer Agent's result of a measurement: plain text, bounded, never truncated.

    The verdict line, the header line (so amber, "not measured" and "not stored" reach the
    agent), each failing row (no excerpt, no suggested command) up to the 20-line cap and a
    count of the rest, then the Markdown and JSON report paths.
    """
    rows = [
        block.text.replace("\n", " ")
        for block in _failing(document, _Names(document))[1]
        if isinstance(block, Text)
    ]
    header = next(block for block in _header(document)[1] if isinstance(block, Text))
    lines = [_verdict_line(document).text, header.text, *rows[:_LIST_CAP]]
    if len(rows) > _LIST_CAP:
        lines.append(f"... and {len(rows) - _LIST_CAP} more failing rows in the Markdown report")
    paths = document["view"]["paths"]
    return "\n".join([*lines, f"Markdown: {paths['markdown']}", f"JSON: {paths['json']}"])
