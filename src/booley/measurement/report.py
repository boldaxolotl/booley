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
words in Markdown, never colour (a rules change names its class, "loosened" and so on,
in its text); integers print whole and floats with ``:.12g`` (Δ signed) so float noise
never reaches the report; in Markdown, specials in user text are backslash-escaped.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from booley.measurement.run_document import is_measured
from booley.presentation.colors import amber, bold, green, red
from booley.presentation.terminal_table import render_table
from booley.runtime.atomic_files import atomic_replace_bytes
from booley.runtime.timefmt import format_duration

_LIST_CAP = 20
_MISSING = "—"
_Tone = Literal["met", "fail", "warn"] | None
_Address = tuple[str, str]  # (identity, flow)


@dataclass(frozen=True)
class _Text:
    text: str
    tone: _Tone = None


@dataclass(frozen=True)
class _Rows:
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    tones: tuple[_Tone, ...]  # per row; colours its last cell on a terminal


@dataclass(frozen=True)
class _Code:
    text: str


_Block = _Text | _Rows | _Code
_Section = tuple[str, list[_Block]]  # title, blocks


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


def _tone(document: Mapping[str, Any]) -> _Tone:
    """Fail for an unmet or error result; green only for a Measured run with nothing amber."""
    counts = document["view"]["counts"]
    if counts["unmet"] or counts["error"]:
        return "fail"
    return "warn" if document["view"]["amber"] or not is_measured(document) else "met"


def _verdict_line(document: Mapping[str, Any]) -> _Text:
    counts = document["view"]["counts"]
    parts = [
        f"{counts['unmet']} unmet" if counts["unmet"] else "",
        _count(counts["error"], "error") if counts["error"] else "",
        f"{counts['not_evaluated']} not evaluated" if counts["not_evaluated"] else "",
    ]
    enforced = counts["enforced"]
    failing = ", ".join(part for part in parts if part)
    text = (
        f"{failing} of {enforced} enforced"
        if failing
        else f"{enforced} of {enforced} enforced met"
    )
    return _Text(text, _tone(document))


def _base_lines(view: Mapping[str, Any]) -> list[_Block]:
    base, rules_base, base_run = view["base"], view["rules_base"], view["base_run"]
    lines: list[_Block]
    if base["commit"] is None:
        lines = [_Text(f"no base: {base['no_base']}")]
    else:
        asked = base["requested"]
        extra = f", requested `{asked}`" if asked not in (None, base["commit"]) else ""
        lines = [_Text(f"Base `{base['commit']}` ({base['choice']}){extra}")]
    if rules_base is not None and rules_base["commit"] is None:
        lines.append(_Text(f"rules: no base: {rules_base['no_base']}"))
    elif rules_base is not None:
        lines.append(_Text(f"rules Base `{rules_base['commit']}` ({rules_base['choice']})"))
    if base_run["problem"] is not None:
        lines.append(_Text(f"base run unreadable: {base_run['problem']}", "warn"))
    elif base_run["base_run_id"] is not None:
        lines.append(_Text(f"base run `{base_run['base_run_id']}`"))
    elif base_run["command"] is not None:
        lines.append(_Text(f"no base run; create it with `{base_run['command']}`"))
    return lines


def _run_lines(document: Mapping[str, Any]) -> list[_Block]:
    """Toolchain, history and what a stopped or cancelled run left undone."""
    toolchain, history = document["toolchain"], document["view"]["history"]
    parts = [] if toolchain["image"] is None else [f"image {toolchain['image']}"]
    parts.append(f"Booley {toolchain['booley']} ({toolchain['booley_kind']})")
    parts.extend([toolchain["booley_note"]] if toolchain["booley_note"] else [])
    parts.extend(f"{tool} {version}" for tool, version in toolchain["tools"].items())
    place = f"run {history['position']} of {history['total']} for this tree"
    if history["earlier"]:
        place += "; earlier: " + ", ".join(r["outcome"] for r in history["earlier"])
    lines: list[_Block] = [_Text(f"Toolchain: {'; '.join(parts)}"), _Text(place)]
    lines.extend(_Text(f"history: {problem}", "warn") for problem in history["problems"])
    if document["status"] == "stopped":
        left = document["view"]["counts"]["not_evaluated"]
        lines.append(_Text(f"stopped early: {left} not evaluated, use `--all`", "warn"))
    elif document["status"] == "cancelled":
        lines.append(_Text("cancelled", "warn"))
    return lines


def _header(document: Mapping[str, Any]) -> _Section:
    view = document["view"]
    commit, tone = view["commit"], _tone(document)
    states = [] if document["status"] == "completed" else [document["status"]]
    states.extend(["failing"] if tone == "fail" else [])
    states.extend(view["amber"])
    states = states or ["measured" if is_measured(document) else "not measured"]
    blocks: list[_Block] = [
        _Text(f"Run {document['run_id']}: {', '.join(states)}", tone),
        _Text(f"Commit `{commit['sha']}`, tree `{commit['tree']}`"),
    ]
    if commit["measured_as"] is not None:
        blocks.append(_Text(f"measured as `{commit['measured_as']}`, identical tree"))
    if commit["booley_tree"] is not None:
        suffix = f" (commit `{commit['booley_commit']}`)" if commit["booley_commit"] else ""
        blocks.append(_Text(f"Booley Directory tree `{commit['booley_tree']}`{suffix}"))
    blocks.extend(_base_lines(view))
    blocks.extend(_run_lines(document))
    blocks.append(_Text(f"Commit message: {commit['subject']}"))
    return ("Run", [*blocks, *([_Code(commit["body"])] if commit["body"] else [])])


def _failing(document: Mapping[str, Any], names: _Names) -> _Section:
    results = {
        (table["identity"], table["flow"], result["key"]): result
        for table in document["tables"]
        for result in table["results"]
    }
    blocks: list[_Block] = []
    for item in document["view"]["excerpts"]:
        address, key = (item["identity"], item["flow"]), item["key"]
        value = _num(results[(*address, key)]["value"])
        bound = names.bound(*address, key)
        blocks.append(
            _Text(f"{names.label(*address)} {key}: {value} vs {bound}, {item['verdict']}", "fail")
        )
        blocks.append(_Code(item["excerpt"] or "(no detail)"))
        blocks.append(_Code(f"booley measure --target {names.target[address]}\nbooley measure"))
    return ("Failing", blocks)


def _metrics(document: Mapping[str, Any], names: _Names) -> _Section:
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
    verdict_tones: dict[str, _Tone] = {"met": "met", "unmet": "fail", "error": "fail"}
    rows: list[tuple[str, ...]] = []
    tones: list[_Tone] = []
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
    return ("Metrics", [_Rows(headers, tuple(rows), tuple(tones))])


def _times(document: Mapping[str, Any], names: _Names) -> _Section:
    lines: list[_Block] = []
    for table in document["tables"]:
        seconds = table["wall_time_s"]
        shown = "not started" if seconds is None else format_duration(seconds)
        lines.append(_Text(f"{names.label(table['identity'], table['flow'])}: {shown}"))
    return ("Wall time", [*lines, _Text(f"total: {format_duration(document['wall_time_s'])}")])


def _change_line(change: Mapping[str, Any]) -> _Text:
    old, new, kind = change["old"], change["new"], f"{change['kind']} ({change['direction']})"
    if old is not None and new is not None:
        detail = kind if old == new else f"{kind}, {_num(old)} → {_num(new)}"
    else:  # a bound or table added or removed, or a tracked entry edited
        detail = ", ".join([kind, *([f"new {_num(new)}"] if new is not None else [])])
        detail += f", was {_num(old)}" if old is not None else ""
    where = " ".join(part for part in (change["target"], change["flow"], change["key"]) if part)
    note = f" — {change['note']}" if change["note"] else ""
    return _Text(f"{where}: {detail}{note}", "warn" if change["direction"] == "loosened" else None)


def _rules_diff(view: Mapping[str, Any]) -> _Section:
    diff = view["rules_diff"]
    ordered = sorted(diff["changes"], key=lambda change: change["direction"] != "loosened")
    blocks: list[_Block] = [_change_line(change) for change in ordered]
    if diff["not_compared"] is not None:
        blocks.append(_Text(f"rules not compared: {diff['not_compared']}"))
    if diff["unreadable"] is not None:
        blocks.append(_Text(f"rules file unreadable: {diff['unreadable']}", "warn"))
    if diff["tests_diff"]:
        blocks.extend([_Text("tests changed", "warn"), _Code(diff["tests_diff"])])
    return ("Rules changes", blocks or [_Text("no rules changes")])


def _warnings(document: Mapping[str, Any], names: _Names) -> _Section:
    view = document["view"]
    base_run = view["base_run"]
    blocks: list[_Block] = [_Text(reason, "warn") for reason in view["amber"]]
    dropped = {(item["identity"], item["flow"]) for item in base_run["tests_dropped"]}
    for item in base_run["tests_dropped"]:
        label = names.label(item["identity"], item["flow"])
        move = f"{_num(item['base_total'])} → {_num(item['total'])}"
        blocks.append(_Text(f"`tests_total` dropped: {label} {move}", "warn"))
    if base_run["toolchain_changes"]:
        changed = ", ".join(base_run["toolchain_changes"])
        blocks.append(_Text(f"toolchain differs from the base run: {changed}", "warn"))
    for item in base_run["worsened"]:
        address = (item["identity"], item["flow"])
        if names.entry[(*address, item["key"])]["metric"] == "tests_total" and address in dropped:
            continue
        move = f"{_num(item['base_value'])} → {_num(item['value'])}"
        blocks.append(
            _Text(f"worse than base: {names.label(*address)} {item['key']} {move}", "warn")
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


def _changed(view: Mapping[str, Any]) -> _Section:
    changed = view["changed"]
    files, listed = ([], []) if changed is None else (changed["files"], [])
    blocks: list[_Block] = [_Text(f"no base: {view['base']['no_base']}")]
    if changed is not None:
        listed = [
            f"{f['status']} {f['path']}" + (" (protected)" if f["protected"] else "")
            for f in files
        ]
        listed.extend(_submodule_line(change) for change in changed["submodules"])
        blocks = [_Text(_counts_line(changed)), *(_Text(line) for line in listed[:_LIST_CAP])]
        if len(listed) > _LIST_CAP:
            blocks.append(_Text(f"... and {len(listed) - _LIST_CAP} more"))
    # Edits are never capped, so the list cannot hide them.
    edits = view["protected_edits"]
    for label, paths in (
        ("protected edits", edits["repository"]),
        ("constraint edits", [f["path"] for f in files if f["constraint"]]),
        ("protected edits in the Booley Directory", edits["booley_directory"]),
    ):
        blocks.extend([_Text(f"{label}: {', '.join(paths)}", "warn")] if paths else [])
    return ("Changed files", blocks)


def _failing_tests(document: Mapping[str, Any], names: _Names) -> _Section:
    blocks: list[_Block] = []
    for table in document["tables"]:
        plain = {} if table["values"] is None else table["values"]["plain"]
        if plain.get("tests_failed", 0) > 0:
            failed, total = _num(plain["tests_failed"]), _num(plain.get("tests_total"))
            label = names.label(table["identity"], table["flow"])
            blocks.append(_Text(f"{label}: {failed} failed of {total} tests", "fail"))
    return ("Failing tests", blocks)


def _footer(document: Mapping[str, Any], names: _Names) -> _Section:
    blocks: list[_Block] = []
    for item in document["view"]["evidence"]:
        label = names.label(item["identity"], item["flow"])
        blocks.extend(_Text(f"{label} report: `{path}`") for path in item["reports"])
        blocks.extend(
            _Text(f"{label}: {report['name']} ({report['bytes']} bytes, not kept)")
            for report in item["dropped"]
        )
    paths = document["view"]["paths"]
    return (
        "Files",
        [*blocks, _Text(f"Markdown: `{paths['markdown']}`"), _Text(f"JSON: `{paths['json']}`")],
    )


def _sections(document: Mapping[str, Any]) -> list[_Section]:
    """Every section in the report's order; one with nothing to say is left out."""
    names, view = _Names(document), document["view"]
    untabled = view["targets_without_table"]
    targets = [_Text(f"Targets with no metrics: {', '.join(untabled)}")] if untabled else []
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
        _footer(document, names),
    ]
    return [section for section in sections if section[1]]


def _fence(text: str) -> str:
    """A backtick fence longer than any backtick run in *text*."""
    longest = max((len(run) for run in re.findall("`+", text)), default=0)
    return "`" * max(3, longest + 1)


_CODE_SPAN = re.compile(r"`[^`\n]*`")
_SPECIAL = re.compile(r"[\\*<>\[\]`]|(?<![A-Za-z0-9])_|_(?![A-Za-z0-9])")


def _md(text: str) -> str:
    """Backslash-escape what Markdown would parse, leaving backtick spans as written."""
    pieces = re.split(f"({_CODE_SPAN.pattern})", text)
    return "".join(
        piece if index % 2 else _SPECIAL.sub(lambda hit: "\\" + hit.group(), piece)
        for index, piece in enumerate(pieces)
    )


def _cell(text: str) -> str:
    return _md(text).replace("|", "\\|").replace("\n", " ")


def _markdown_chunks(blocks: Sequence[_Block]) -> list[str]:
    """One chunk per table or fence; consecutive text lines form one list."""
    chunks: list[str] = []
    for block in blocks:
        if isinstance(block, _Text):
            item = "- " + _md(block.text).replace("\n", " ")
            if chunks and chunks[-1].startswith("- "):
                chunks[-1] += "\n" + item
            else:
                chunks.append(item)
        elif isinstance(block, _Rows):
            lines = [block.headers, ("---",) * len(block.headers), *block.rows]
            chunks.append("\n".join("| " + " | ".join(map(_cell, row)) + " |" for row in lines))
        else:
            fence = _fence(block.text)
            chunks.append(f"{fence}\n{block.text}\n{fence}")
    return chunks


def markdown_report(document: Mapping[str, Any]) -> str:
    """The Markdown Measurement Report of a view JSON (``run_view`` output)."""
    parts = ["# Measurement Report", f"**{_verdict_line(document).text}**"]
    for title, blocks in _sections(document):
        parts.extend([f"## {title}", *_markdown_chunks(blocks)])
    return "\n\n".join(parts) + "\n"


def write_markdown_report(document: Mapping[str, Any]) -> Path:
    """Publish the Markdown report atomically at the view's ``paths.markdown``; last write wins."""
    path = Path(document["view"]["paths"]["markdown"])
    atomic_replace_bytes(path, markdown_report(document).encode("utf-8"), mode=0o644)
    return path


def _painter(tone: _Tone) -> Callable[[str], str]:
    return {"met": green, "fail": red, "warn": amber}.get(tone or "", str)


def _terminal_lines(block: _Block, width: int | None) -> list[str]:
    if isinstance(block, _Text):
        return ["  " + _painter(block.tone)(block.text.replace("\n", " "))]
    if isinstance(block, _Code):
        return ["    " + line for line in block.text.splitlines()]
    rows = [
        (*row[:-1], _painter(tone)(row[-1]))
        for row, tone in zip(block.rows, block.tones, strict=True)
    ]
    return render_table(block.headers, rows, width=width)


def terminal_report(document: Mapping[str, Any], *, width: int | None = None) -> str:
    """The terminal Measurement Report of a view JSON; plain text when colour is off."""
    verdict = _verdict_line(document)
    lines = [bold("Measurement Report"), _painter(verdict.tone)(verdict.text)]
    for title, blocks in _sections(document):
        lines.extend(["", bold(title)])
        lines.extend(line for block in blocks for line in _terminal_lines(block, width))
    return "\n".join(lines) + "\n"
