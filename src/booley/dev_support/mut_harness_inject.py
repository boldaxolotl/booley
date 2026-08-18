"""SystemVerilog source-editing engine for the mutation-tester harness.

This module owns the *textual* injection of the mutation harness into a DUT
top file — everything that parses and rewrites RTL source.  Two edits are
applied to the DUT top:

  1. A file-scope ``package booley_mut_pkg ... endpackage`` prepended before
     the first module.
  2. An in-module ``import + initial`` reader block that samples the
     ``+MUT_ID=k`` plusarg at runtime.

Both edits are wrapped in marker comments so the injection is idempotent and
symmetrically removable (see :func:`remove_mut_harness`).

Split out of ``mutation_lock`` (principle 8 / Single Responsibility): the
lock module manages persistent lock *state*; this module manages the SV
source rewrite.  The dependency is one-way — ``mutation_lock`` re-exports the
public names here for backward compatibility, but nothing here imports back
from ``mutation_lock``.
"""

from __future__ import annotations

import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Package + plusarg-reader generation
# ---------------------------------------------------------------------------


# The package body is harness-controlled — the agent never touches it.  Bumping
# it is breaking: bump LOCK_SCHEMA_VERSION at the same time so old locks
# invalidate.
#
# Implementation note: rather than carry a separate booley_mut_pkg.sv file
# (which would need wiring into the per-project file list in configs.toml),
# we **prepend** the package source to the DUT top file as a textual edit.
# SystemVerilog file scope permits ``package ... endpackage`` before the
# module declaration, and Verilator + Icarus both accept it.  The lock dir
# still preserves a copy of the package text for diagnostics.
_MUT_PKG_TEMPLATE = """\
package booley_mut_pkg;
{time_decls}\
  int mut_id = 0;
endpackage
"""

# Time literals as they appear in `timescale / timeunit / timeprecision.
_TIME_LITERAL = r"[0-9]+(?:\.[0-9]+)?\s*[munpf]?s"
_TIMEUNIT_RE = re.compile(
    rf"^[ \t]*timeunit\s+({_TIME_LITERAL})\s*(?:/\s*({_TIME_LITERAL})\s*)?;",
    re.MULTILINE,
)
_TIMEPRECISION_RE = re.compile(
    rf"^[ \t]*timeprecision\s+({_TIME_LITERAL})\s*;",
    re.MULTILINE,
)
_TIMESCALE_RE = re.compile(
    rf"^[ \t]*`timescale\s+({_TIME_LITERAL})\s*/\s*({_TIME_LITERAL})",
    re.MULTILINE,
)

# A widespread convention keeps the project's `timescale in one header that
# every source `includes.  We do not resolve includes, so the *value* is
# unknown — but the fact that one is coming is not.
_TIMESCALE_INCLUDE_RE = re.compile(
    r"^[ \t]*`include\s+[\"<]([^\">]*timescale[^\">]*)[\">]",
    re.MULTILINE | re.IGNORECASE,
)

# First design element in the file — where an inherited directive would start
# to matter to the design rather than only to our package.
_DESIGN_ELEMENT_RE = re.compile(
    r"^[ \t]*(?:module|interface|program|package|primitive|checker)\b",
    re.MULTILINE,
)

# Value used only when the design's real one is behind an `include.  The
# package declares a single ``int`` and nothing that consumes time, so any
# unit silences TIMESCALEMOD without changing behaviour; the included header
# re-establishes the design's own value immediately below our block.
_DEFAULT_TIMESCALE_DIRECTIVE = "`timescale 1ns / 1ps\n"


def _norm_time(literal: str) -> str:
    """Collapse ``1 ns`` / ``1\\tns`` to the canonical ``1ns``."""
    return re.sub(r"\s+", "", literal)


def _timescale_prelude(dut_text: str) -> tuple[str, str]:
    """Derive the package's time declarations from the DUT top it rides on.

    Returns ``(directive_before_pkg, declarations_inside_pkg)``.

    A timescale-less package is fatal on a design that declares time units
    everywhere: Verilator's TIMESCALEMOD ("this unit has no timescale, others
    do") is promoted to an error under ``-Wall``/``--Werror`` and elaboration
    dies before a single mutation runs (SETUP-F-37).  Hardcoding a unit would
    be just as wrong — it must match the design — so we mirror whatever the
    DUT top itself declares:

      - ``timeunit``/``timeprecision`` declarations  -> same declarations in
        the package body (no directive state is touched, so nothing downstream
        of the injection point changes).
      - a bare ```timescale`` directive              -> the same directive
        emitted *above* the package, since the file's own directive sits below
        our prepended block and would otherwise leave the package uncovered.
        The original directive re-establishes an identical value right after.
      - a ```include`` of a *timescale-named* header -> a default directive
        above the package.  The design's real value is inside the header,
        which we deliberately do not resolve; the package holds no
        time-consuming code, so the unit it compiles under is immaterial and
        only its presence matters.  Emitted only when that include precedes
        the file's first design element, which guarantees the header restores
        the design's own value before anything that could inherit ours.
      - neither                                      -> nothing, which is
        correct: a design with no timescale anywhere never trips TIMESCALEMOD.

    Any *other* unresolved ```include`` is left alone on purpose.  We cannot
    tell whether it carries a directive, and guessing costs more than it
    saves: a speculative ```timescale`` above a design element that has no
    directive of its own would silently redefine the design's time unit, which
    is a worse failure than the lint warning it would suppress.
    """
    # Ignore a previously injected block so re-injection is idempotent.
    body, _ = _strip_block(dut_text, _PKG_MARKER_BEGIN, _PKG_MARKER_END)
    unit_match = _TIMEUNIT_RE.search(body)
    if unit_match:
        decls = f"  timeunit {_norm_time(unit_match.group(1))};\n"
        # ``timeunit 1ns/1ps;`` carries the precision inline; otherwise look
        # for a separate ``timeprecision`` declaration.
        precision = unit_match.group(2)
        if not precision:
            prec_match = _TIMEPRECISION_RE.search(body)
            precision = prec_match.group(1) if prec_match else ""
        if precision:
            decls += f"  timeprecision {_norm_time(precision)};\n"
        return "", decls
    ts_match = _TIMESCALE_RE.search(body)
    if ts_match:
        unit, precision = (_norm_time(g) for g in ts_match.groups())
        return f"`timescale {unit} / {precision}\n", ""
    inc_match = _TIMESCALE_INCLUDE_RE.search(body)
    if inc_match and _precedes_first_design_element(body, inc_match.start()):
        return _DEFAULT_TIMESCALE_DIRECTIVE, ""
    return "", ""


def _precedes_first_design_element(text: str, pos: int) -> bool:
    """True when offset *pos* sits above the file's first module/package/etc.

    Only then can a directive we emit be safely overridden before any design
    code inherits it.
    """
    first = _DESIGN_ELEMENT_RE.search(text)
    return first is None or pos < first.start()


def generate_mut_pkg(dut_text: str = "") -> str:
    """Return the booley_mut_pkg source text for a DUT top's *dut_text*.

    *dut_text* supplies the design's time declarations (see
    ``_timescale_prelude``); an empty string yields the timescale-free package,
    which is what a design without time declarations wants.
    """
    _directive, decls = _timescale_prelude(dut_text)
    return _MUT_PKG_TEMPLATE.format(time_decls=decls)


# Markers used to make the injection idempotent and removable.  Two distinct
# markers — one for the file-level package, one for the in-module reader —
# because they live in different scopes and may end up far apart in the file
# after the agent edits modules above and below the reader.
_PKG_MARKER_BEGIN = "// __BOOLEY_MUT_PKG_BEGIN__"
_PKG_MARKER_END = "// __BOOLEY_MUT_PKG_END__"
_READER_MARKER_BEGIN = "// __BOOLEY_MUT_READER_BEGIN__"
_READER_MARKER_END = "// __BOOLEY_MUT_READER_END__"

# Printed by the injected reader on every *mutant* run.  It is the only
# runtime proof that ``+MUT_ID=k`` actually reached the design, which is what
# lets a 0-killed sweep be diagnosed as "the tests don't cover this scope"
# instead of "the harness is broken" (SETUP-F-38).  Deliberately silent on the
# baseline so a MUT_ID=0 run stays byte-identical to an unmutated one — some
# testbenches diff their stdout against a golden log.
MUT_ECHO_PREFIX = "[booley_mut] MUT_ID="

_PLUSARG_READER_TEMPLATE = f"""\
{_READER_MARKER_BEGIN}
  import booley_mut_pkg::*;
  initial begin : __booley_mut_plusarg_reader
    int __mut_id_val;
    if ($value$plusargs("MUT_ID=%d", __mut_id_val)) begin
      mut_id = __mut_id_val;
      if (mut_id != 0) $display("{MUT_ECHO_PREFIX}%0d active", mut_id);
    end
  end
{_READER_MARKER_END}
"""

_VERILOG_READER_TEMPLATE = f"""\
{_READER_MARKER_BEGIN}
  integer mut_id = 0;
  integer __mut_id_val;
  initial begin : __booley_mut_plusarg_reader
    if ($value$plusargs("MUT_ID=%d", __mut_id_val)) begin
      mut_id = __mut_id_val;
      if (mut_id != 0) $display("{MUT_ECHO_PREFIX}%0d active", mut_id);
    end
  end
{_READER_MARKER_END}
"""


def _wrap_pkg_block(dut_text: str = "") -> str:
    """Return the package source (+ any timescale directive) inside markers.

    The DECLFILENAME suppression is structural, not a style preference: the
    package is inlined into ``<dut_top>.sv``, so its name can never match the
    filename and a project that lints its sim build with ``-Wall`` would fail
    elaboration on the harness itself. Plain comments suffice for every other EDA tool.
    """
    directive, _decls = _timescale_prelude(dut_text)
    return (
        f"{_PKG_MARKER_BEGIN}\n"
        f"{directive}"
        "/* verilator lint_off DECLFILENAME */\n"
        f"{generate_mut_pkg(dut_text)}"
        "/* verilator lint_on DECLFILENAME */\n"
        f"{_PKG_MARKER_END}\n"
    )


def generate_plusarg_reader_snippet() -> str:
    """Return the initial-block source that reads ``+MUT_ID=k`` at runtime.

    Idempotency is enforced by surrounding marker comments — see
    ``inject_mut_harness``.
    """
    return _PLUSARG_READER_TEMPLATE


def _skip_module_header_imports(text: str, start: int) -> int:
    """Skip package imports that SystemVerilog permits after a module name."""
    import re

    pattern = re.compile(
        r"(?:\s|//[^\n]*(?:\n|$)|/\*.*?\*/)*import\b[^;]*;",
        re.DOTALL,
    )
    offset = start
    while match := pattern.match(text, offset):
        offset = match.end()
    return offset


# ``timeunit``/``timeprecision`` at the top of a module body, plus whatever
# comments/whitespace precede them.
_LEADING_TIME_DECL_RE = re.compile(
    r"(?:\s|//[^\n]*(?:\n|$)|/\*.*?\*/)*(?:timeunit|timeprecision)\b[^;]*;",
    re.DOTALL,
)


def _skip_leading_time_declarations(text: str, start: int) -> int:
    """Skip time declarations at the head of a module body.

    SystemVerilog requires ``timeunit``/``timeprecision`` to precede every
    other item in their scope, so dropping the ``import`` + ``initial`` reader
    above them makes the module illegal — the same class of breakage as a
    timescale-less package on a design that declares time units everywhere
    (SETUP-F-37).
    """
    offset = start
    while match := _LEADING_TIME_DECL_RE.match(text, offset):
        offset = match.end()
    return offset


def _find_module_body_insertion_point(text: str, top_module: str) -> int | None:
    """Return character offset just after the ``module <top> ... ;`` header.

    The insertion point is *after* the trailing semicolon of the module
    declaration and after any leading ``timeunit``/``timeprecision``
    declarations — i.e. the first place a new item may legally go.  We use a
    deliberately
    narrow regex and a brace/paren counter rather than a full SV parser:

      - Matches: ``module <top>`` optionally followed by ``#(...)`` params
        and ``(...)`` ports, terminated by ``;``.
      - Skips block comments and line comments while scanning for the
        terminating semicolon — strings inside a port list with a literal
        ``;`` are vanishingly rare in port declarations but we honour
        them defensively.

    Returns None when no module header matches.  Callers must treat this
    as a hard failure: cold-start aborts.
    """
    import re as _re

    pattern = _re.compile(
        rf"\bmodule\s+{_re.escape(top_module)}\b",
        _re.IGNORECASE,
    )
    m = pattern.search(text)
    if not m:
        return None
    # SystemVerilog permits package imports between the module name and its
    # parameter/port lists: ``module top import pkg::*; #( ... ) ( ... );``.
    # Their semicolons are part of the header, so begin scanning after them.
    i = _skip_module_header_imports(text, m.end())
    n = len(text)
    paren_depth = 0
    while i < n:
        ch = text[i]
        # Skip block comment
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            close = text.find("*/", i + 2)
            if close < 0:
                return None
            i = close + 2
            continue
        # Skip line comment
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            nl = text.find("\n", i)
            if nl < 0:
                return n
            i = nl + 1
            continue
        # Skip string
        if ch == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 2
                    continue
                j += 1
            i = j + 1
            continue
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth -= 1
        elif ch == ";" and paren_depth == 0:
            # Past the header — but time declarations must stay first in the
            # body, so the reader goes below them.
            return _skip_leading_time_declarations(text, i + 1)
        i += 1
    return None


class MutHarnessInjectionError(RuntimeError):
    """Raised when the mut-id harness cannot be inserted into the DUT top."""


def _strip_block(text: str, begin: str, end: str) -> tuple[str, bool]:
    """Remove every ``begin ... end`` marker pair from *text*.

    Returns the new text and a flag indicating whether at least one block
    was stripped.  Tolerant of trailing newlines around either marker so a
    re-inject leaves no doubled blank lines.
    """
    changed = False
    out = text
    while True:
        b = out.find(begin)
        if b < 0:
            break
        e = out.find(end, b + len(begin))
        if e < 0:
            # Unbalanced — drop just the begin line and bail to avoid an
            # infinite loop.
            nl = out.find("\n", b)
            cut = nl + 1 if nl >= 0 else len(out)
            out = out[:b] + out[cut:]
            changed = True
            break
        cut_end = e + len(end)
        if cut_end < len(out) and out[cut_end] == "\n":
            cut_end += 1
        # Also consume one leading newline before the begin marker so the
        # file doesn't accumulate blank lines across inject/remove cycles.
        cut_start = b
        if cut_start > 0 and out[cut_start - 1] == "\n":
            cut_start -= 1
        out = out[:cut_start] + out[cut_end:]
        changed = True
    return out, changed


def inject_mut_harness(
    dut_top_path: Path,
    top_module: str,
) -> tuple[bool, bool]:
    """Inject the booley_mut_pkg + plusarg-reader harness into *dut_top_path*.

    Two textual edits in one call:
      1. Prepend ``package booley_mut_pkg ... endpackage`` at file scope.
      2. Insert ``import + initial`` reader block at the start of the
         ``module <top_module>`` body.

    Returns ``(pkg_inserted, reader_inserted)`` — both False when both
    blocks were already present (idempotent re-call).  Raises
    ``MutHarnessInjectionError`` when the module header cannot be located.

    The injection is symmetric with ``remove_mut_harness`` via marker
    comments, so a partial injection (pkg in but reader out) is recoverable.
    """
    text = dut_top_path.read_text(encoding="utf-8")

    verilog = dut_top_path.suffix.lower() == ".v"
    pkg_inserted = False
    if not verilog and _PKG_MARKER_BEGIN not in text:
        # The package inherits its time declarations from this very file — see
        # ``_timescale_prelude`` (SETUP-F-37).
        text = _wrap_pkg_block(text) + text
        pkg_inserted = True

    reader_inserted = False
    if _READER_MARKER_BEGIN not in text:
        # Re-locate after the prepend so the offset is correct.
        insertion = _find_module_body_insertion_point(text, top_module)
        if insertion is None:
            # Undo the package prepend so we don't leave a partial edit on
            # disk — the caller can retry without corruption.
            if pkg_inserted:
                text, _ = _strip_block(text, _PKG_MARKER_BEGIN, _PKG_MARKER_END)
                dut_top_path.write_text(text, encoding="utf-8")
            raise MutHarnessInjectionError(
                f"could not locate 'module {top_module}' header in "
                f"{dut_top_path}; mut-id harness insertion aborted "
                f"(DUT top file probably uses an unusual coding style — "
                f"investigate or pin a different top).",
            )
        snippet = "\n" + (
            _VERILOG_READER_TEMPLATE if verilog else generate_plusarg_reader_snippet()
        )
        text = text[:insertion] + snippet + text[insertion:]
        reader_inserted = True

    if pkg_inserted or reader_inserted:
        dut_top_path.write_text(text, encoding="utf-8")
    return pkg_inserted, reader_inserted


def remove_mut_harness(dut_top_path: Path) -> bool:
    """Strip both harness blocks from *dut_top_path*.

    Returns True when at least one block was removed; False when the file
    is missing or contained no markers.  Used on cleanup paths where a
    ``git checkout`` isn't available (DUT top outside scope, or worktree
    in a weird state).
    """
    if not dut_top_path.exists():
        return False
    text = dut_top_path.read_text(encoding="utf-8")
    text, pkg_changed = _strip_block(text, _PKG_MARKER_BEGIN, _PKG_MARKER_END)
    text, reader_changed = _strip_block(
        text,
        _READER_MARKER_BEGIN,
        _READER_MARKER_END,
    )
    if pkg_changed or reader_changed:
        dut_top_path.write_text(text, encoding="utf-8")
        return True
    return False
