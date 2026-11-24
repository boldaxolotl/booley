"""The Review Report: Markdown and terminal views of a Review Run chain (issue #5455).

The chain is a discovery Review Run, the verify Review Runs of it that lead to the reported
commit (oldest first), and the reported run last; a discovery run alone is its own chain.
Both views render from the runs' JSON alone (no store, repository or clock) out of one
content model, so they hold the same facts in the same order: the state, the Run, the
Waivers, what Needs you, what is Fixed, what is non-blocking, and the reviews not needed.

A finding is blocking exactly when it was introduced, is still open in the reported run and
is at or above its review's ``resolve`` (:func:`booley.specialists.reviewer.at_or_above`,
the rule ``--fail-on`` applies). A review whose re-check errored judged nothing, so its
findings keep the state of the last run that judged them, never "fixed". A section with
nothing to show is left out; the Run section and the state line are always shown.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from booley.evidence.review_vocabulary import OPEN_STATUSES, STATUS_FIXED, STATUS_WAIVED
from booley.presentation.colors import bold
from booley.presentation.report_blocks import (
    Block,
    Code,
    Section,
    Text,
    Tone,
    markdown_sections,
    paint,
    terminal_sections,
)
from booley.specialists.reviewer import at_or_above

_SHORT = 12  # hex digits of a commit shown in the report
_Run = Mapping[str, Any]
_Key = tuple[str, str, str | None, str]  # role, focus, spec, resolve


@dataclass(frozen=True)
class _Finding:
    """One introduced finding of the discovery run, as the reported run leaves it."""

    row: _Run  # the latest judged row
    review: _Run  # the discovery review that found it
    fixed_in: tuple[str, ...]  # the commits a fix is credited to, when the row is fixed

    @property
    def is_open(self) -> bool:
        return self.row["status"] in OPEN_STATUSES

    @property
    def blocking(self) -> bool:
        return self.is_open and at_or_above(self.row["severity"], self.review["resolve"])


def _key(review: _Run) -> _Key:
    return (review["role"], review["focus"], review["spec"], review["resolve"])


def _judged(run: _Run, key: _Key, finding_id: str) -> _Run | None:
    """The row of *finding_id* in *run*; ``None`` when *run* judged nothing of it.

    A verify run has no review for a finding it was not asked about, and a review whose
    re-check errored carries the discovery rows over without judging them.
    """
    for review in run["reviews"]:
        if _key(review) == key and review["outcome"] != "error":
            return next((r for r in review["introduced"] if r["finding_id"] == finding_id), None)
    return None


def _fix_commits(chain: Sequence[_Run], rows: Sequence[_Run | None]) -> tuple[str, ...]:
    """The commits of the first judging run after the last judged run that did not fix it."""
    judged = [(run, row) for run, row in zip(chain, rows, strict=True) if row is not None]
    last_open = max(i for i, (_, row) in enumerate(judged) if row["status"] != STATUS_FIXED)
    earlier = judged[last_open][0].get("commits", [])
    return tuple(c for c in judged[last_open + 1][0].get("commits", []) if c not in earlier)


def _findings(chain: Sequence[_Run]) -> list[_Finding]:
    """Every introduced finding of the discovery run with its state in the reported run."""
    found: list[_Finding] = []
    for review in chain[0]["reviews"]:
        for first in review["introduced"]:
            rows = [
                first if index == 0 else _judged(run, _key(review), first["finding_id"])
                for index, run in enumerate(chain)
            ]
            latest = rows[-1] or first
            fixed = latest["status"] == STATUS_FIXED
            found.append(_Finding(latest, review, _fix_commits(chain, rows) if fixed else ()))
    return found


def _short(commit: str) -> str:
    return commit[:_SHORT]


def _where(row: _Run) -> str:
    return f"`{row['finding_id']}` {row['file']}:{row['line']} {row['severity']}"


def _review_name(review: _Run) -> str:
    return f"{review['role']} {review['focus']}"


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _state(chain: Sequence[_Run], findings: Sequence[_Finding], *, stored: bool) -> Text:
    """Whether the reported commit is Reviewed, in words; the terminal also colours it."""
    run = chain[-1]
    needs = sum(finding.blocking for finding in findings)
    if any(review["outcome"] == "error" for review in run["reviews"]):
        return Text("Not Reviewed: a review failed", "fail")
    if needs:
        verb = "needs" if needs == 1 else "need"
        return Text(f"Not Reviewed: {_count(needs, 'finding')} {verb} you", "fail")
    if not stored:
        return Text("Not Reviewed: not stored (--target)", "warn")
    if run["outcome"] == "nothing-to-review":
        return Text(
            "Reviewed: nothing to review: no Declared Review has a changed file since the Base",
            "met",
        )
    return Text("Reviewed", "met")


def _run_section(chain: Sequence[_Run]) -> Section:
    discovery, run = chain[0], chain[-1]
    base = run["base"]
    blocks: list[Block] = [
        Text(f"{run['kind']} Review Run {run['run_id']} of `{_short(run['commit'])}`")
    ]
    if run is not discovery:
        blocks.append(
            Text(
                f"verifies discovery Review Run {discovery['run_id']} "
                f"of `{_short(discovery['commit'])}`"
            )
        )
    earlier = [earlier["run_id"] for earlier in chain[1:-1]]
    if earlier:
        blocks.append(Text(f"earlier verify Review Runs: {', '.join(earlier)}"))
    asked = base["requested"]
    extra = f", requested `{_short(asked)}`" if asked not in (None, base["commit"]) else ""
    blocks.append(Text(f"Base `{_short(base['commit'])}` ({base['choice']}){extra}"))
    if run["rules_changed"]:
        changed = ", ".join(run["rules_changed"])
        blocks.append(Text(f"rules changed since the Base: {changed}", "warn"))
    subject, _, body = run["message"].strip().partition("\n")
    blocks.append(Text(f"Commit message: {subject}"))
    return ("Run", [*blocks, *([Code(body.strip())] if body.strip() else [])])


_WAIVER_WORDS: dict[bool | None, tuple[str, Tone]] = {
    True: ("accepted", "warn"),
    False: ("refused", "fail"),
    None: ("not judged", "warn"),
}


def _waivers(chain: Sequence[_Run]) -> Section:
    """Every waiver of every verify run of the chain, oldest first."""
    discovered = {
        r["finding_id"]: r for review in chain[0]["reviews"] for r in review["introduced"]
    }
    blocks: list[Block] = []
    for run in chain[1:]:
        for review in run["reviews"]:
            for waiver in review["waivers"]:
                row = discovered[waiver["finding_id"]]
                word, tone = _WAIVER_WORDS[waiver["accepted"]]
                ruling = f"; ruling: {waiver['ruling']}" if waiver["ruling"] else ""
                text = (
                    f"{_where(row)} waiver {word}: {row['summary']}; "
                    f"justification: {waiver['justification']}{ruling}; "
                    f"verify Review Run {run['run_id']}"
                )
                blocks.append(Text(text, tone))
    return ("Waivers", blocks)


def _line(finding: _Finding, word: str, tail: str = "") -> str:
    return f"{_where(finding.row)} {word}: {finding.row['summary']}{tail}"


def _blocking_line(finding: _Finding) -> Text:
    review = finding.review
    tail = f" ({_review_name(review)}, resolve {review['resolve']})"
    return Text(_line(finding, "blocking", tail), "fail")


def _fixed_line(finding: _Finding) -> Text:
    where = ", ".join(f"`{_short(c)}`" for c in finding.fixed_in)
    return Text(
        _line(finding, f"fixed in {where}" if where else "fixed on the reviewed commit"), "met"
    )


def _non_blocking_line(finding: _Finding) -> Text:
    status = finding.row["status"]
    return Text(
        _line(finding, "non-blocking" + ("" if finding.is_open else f", {status}")), "warn"
    )


def _sections(chain: Sequence[_Run], findings: Sequence[_Finding]) -> list[Section]:
    """Every section in the report's order; one with nothing to say is left out."""
    reviews = chain[0]["reviews"]
    shown = [f for f in findings if f.row["status"] != STATUS_WAIVED]
    sections: list[Section] = [
        _run_section(chain),
        _waivers(chain),
        ("Needs you", [_blocking_line(f) for f in shown if f.blocking]),
        ("Fixed", [_fixed_line(f) for f in shown if f.row["status"] == STATUS_FIXED]),
        (
            "Non-blocking: introduced",
            [
                _non_blocking_line(f)
                for f in shown
                if not f.blocking and f.row["status"] != STATUS_FIXED
            ],
        ),
        (
            "Non-blocking: pre-existing",
            [
                Text(f"{_where(row)} pre-existing: {row['summary']}")
                for review in reviews
                for row in review["pre_existing"]
            ],
        ),
        (
            "Reviews not needed",
            [
                Text(f"{_review_name(review)}: no changed file")
                for review in reviews
                if review["outcome"] == "not-needed"
            ],
        ),
    ]
    return [section for section in sections if section[1]]


def markdown_report(chain: Sequence[_Run]) -> str:
    """The Markdown Review Report of *chain*: the discovery run, its verify runs, the reported run."""
    findings = _findings(chain)
    parts = ["# Review Report", f"**{_state(chain, findings, stored=True).text}**"]
    parts.extend(markdown_sections(_sections(chain, findings)))
    return "\n\n".join(parts) + "\n"


def terminal_report(chain: Sequence[_Run], *, stored: bool = True) -> str:
    """The terminal Review Report of *chain*; plain text when colour is off.

    *stored* is false for a ``--target`` run, which is never Reviewed.
    """
    findings = _findings(chain)
    state = _state(chain, findings, stored=stored)
    lines = [bold("Review Report"), paint(state.tone)(state.text)]
    lines.extend(terminal_sections(_sections(chain, findings), None))
    return "\n".join(lines) + "\n"
