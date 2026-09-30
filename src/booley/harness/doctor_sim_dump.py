"""Static scan tying a Verilator C++ main's hardcoded dump name to ``trace_files``.

A Verilator sim Target that owns its C++ ``main()`` writes its waveform under a
name the source hardcodes (``tfp->open("sim.vcd")``). ``sim --trace`` adopts that
file only when ``[flows.sim].trace_files`` declares it, so an undeclared name
makes the run inconclusive. This module finds those literals and decides, with
the same glob semantics the runtime uses, whether the configured patterns cover
them. Pure functions: no Doctor sink or config reads live here.
"""

from __future__ import annotations

import fnmatch
import json
import posixpath
import re
from pathlib import Path, PurePosixPath

from booley.targets.domain import TargetInspection

# File types whose text can hold the tracer's ``open("...")`` call.
_CPP_FILE_TYPES = frozenset({"cppSource", "cSource"})
# A main larger than this is not a hand-written testbench driver; skip it.
_MAX_SCANNED_BYTES = 1 << 20
_TRACER_RE = re.compile(r"\bVerilated(?:Vcd|Fst)C\b")
# Keep string literals whole, drop comments (so "//" inside a string survives).
_TOKEN_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|/\*.*?\*/|//[^\n]*', re.DOTALL)
_OPEN_RE = re.compile(
    r'(?:->|\.)\s*open\s*\(\s*"([^"\\\n]+\.(?:vcd|fst))"\s*[,)]',
    re.IGNORECASE,
)


def _strip_comments(text: str) -> str:
    return _TOKEN_RE.sub(lambda m: m.group(0) if m.group(0).startswith('"') else " ", text)


def dump_literals_in(text: str) -> tuple[str, ...]:
    """Return literal dump names a Verilator tracer opens in C++ *text*.

    Only files mentioning ``VerilatedVcdC``/``VerilatedFstC`` are scanned. A
    tracer file that also ``.open("x.vcd")``s something unrelated is an accepted
    false positive; non-literal arguments yield nothing (silence beats a guess).
    """
    stripped = _strip_comments(text)
    if not _TRACER_RE.search(stripped):
        return ()
    return tuple(dict.fromkeys(_OPEN_RE.findall(stripped)))


def owned_main_dump_literals(root: Path, inspection: TargetInspection) -> tuple[str, ...]:
    """Scan a Target's C/C++ inputs for hardcoded tracer output names."""
    found: list[str] = []
    for item in inspection.inputs:
        if item.file_type not in _CPP_FILE_TYPES:
            continue
        path = root / item.path
        try:
            if path.stat().st_size > _MAX_SCANNED_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        found.extend(dump_literals_in(text))
    return tuple(dict.fromkeys(found))


def _segments_match(pattern: tuple[str, ...], parts: tuple[str, ...]) -> bool:
    """Match glob *pattern* segments to path *parts* like ``Path.glob``.

    ``*``/``?``/``[..]`` stay within one segment; ``**`` spans zero or more.
    """
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        return any(_segments_match(rest, parts[skip:]) for skip in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _segments_match(rest, parts[1:])


def _glob_covers(pattern: str, path: str) -> bool:
    pattern_parts = PurePosixPath(posixpath.normpath(pattern)).parts
    return _segments_match(pattern_parts, PurePosixPath(posixpath.normpath(path)).parts)


def _absolute_form(value: str, base: str) -> str:
    return posixpath.normpath(value if posixpath.isabs(value) else posixpath.join(base, value))


def literal_is_declared(
    literal: str,
    patterns: list[str],
    *,
    project_root: Path,
    run_cwd: str,
    run_cwd_is_template: bool,
) -> bool:
    """True when some ``trace_files`` pattern would find *literal* at run time.

    The literal is relative to the process cwd (``run_cwd``). Relative patterns
    are tried against it directly; anything that only an absolute comparison can
    decide is compared under a literal ``run_cwd`` and assumed covered under a
    templated one (undecidable statically, and Doctor must never cry wolf).
    """
    literal_absolute = posixpath.isabs(literal)
    base = "" if run_cwd_is_template else _absolute_form(run_cwd, project_root.as_posix())
    for pattern in patterns:
        pattern_absolute = posixpath.isabs(pattern)
        if literal_absolute and pattern_absolute:
            if _glob_covers(pattern, literal):
                return True
        elif literal_absolute:
            continue
        elif not pattern_absolute and _glob_covers(pattern, literal):
            return True
        elif run_cwd_is_template:
            if pattern_absolute or ".." in PurePosixPath(pattern).parts:
                return True
        elif _glob_covers(_absolute_form(pattern, base), _absolute_form(literal, base)):
            return True
    return False


def trace_files_fix(existing: list[str], undeclared: list[str]) -> str:
    """Render the exact ``booley.toml`` line: existing entries kept, new ones added."""
    entries = list(dict.fromkeys([*existing, *sorted(undeclared)]))
    return (
        "add to [flows.sim] in booley.toml: trace_files = ["
        + ", ".join(json.dumps(entry) for entry in entries)
        + "]"
    )
