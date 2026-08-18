"""Golden-file (snapshot) test infrastructure — convention and shared helper.

Why golden tests
----------------
The existing generator tests assert *substrings* of the produced TCL/argv.
A silently **missing** line is invisible to a substring assert — exactly how
two real bugs slipped through:

* ``519842f`` — the generated OpenROAD script lacked ``make_tracks`` after
  ``initialize_floorplan``, so every placement run died with PPL-0021 (and
  silently fell back to OpenSTA).
* ``ca5adaf`` — asic_synthesize emitted a bare ``--sdc`` flag (run_yosys_syn's
  ``--sdc`` takes a value), crashing argparse deep inside the sandbox.

A golden test snapshots the FULL generated output, so any added, removed, or
edited line surfaces as a unified diff at review time.

Convention (locked)
-------------------
* Expected outputs are checked in under ``tests/golden/expected/``.
* On mismatch, the test fails with a unified diff plus a regen hint.
* On a missing golden file, the test fails with the regen hint.
* Regeneration: run with ``BOOLEY_REGEN_GOLDEN=1`` in the environment. The
  helper rewrites the expected file **and still fails the test** with a
  "golden regenerated" message — you must re-run *without* the env var to
  verify. That forced clean re-run is deliberate: it makes every regen an
  explicit two-step act and catches nondeterministic generators (a generator
  that embeds a timestamp/tmp dir can never go green this way).

Determinism / normalization
---------------------------
Golden inputs must be canned and stable: fixed fake paths, no timestamps, no
``tmp_path`` leakage.  Generators that *write files* (the ``write_*`` family)
need a real writable directory, so tests run them against pytest's
``tmp_path`` and then normalize every embedded absolute work-dir to the
stable ``<WORK>`` placeholder via :func:`normalize_work_dir` **before**
comparing/regenerating.  As a last-resort leak guard,
:func:`assert_matches_golden` refuses any text that still contains a
pytest-tmp-smelling path.
"""

from __future__ import annotations

import difflib
import os
import re
from pathlib import Path

import pytest

#: Env var that switches the helper into regenerate mode (see module docstring).
REGEN_ENV = "BOOLEY_REGEN_GOLDEN"

#: Root of the checked-in expected outputs.
EXPECTED_DIR = Path(__file__).resolve().parent / "expected"

#: Stable placeholder substituted for the per-run pytest tmp work dir.
WORK_PLACEHOLDER = "<WORK>"

# Last-resort tmp-leak detector: pytest tmp dirs look like
# /tmp/pytest-of-<user>/pytest-<n>/<test>0 (or a TMPDIR-relocated variant that
# still contains "pytest-of-"/"/pytest-").  Any hit means a path escaped
# normalize_work_dir and the golden would differ from machine to machine.
_TMP_LEAK_RE = re.compile(r"pytest-of-|/pytest-\d|/tmp/")


def normalize_work_dir(text: str, work_dir: Path | str) -> str:
    """Replace every spelling of *work_dir* in *text* with ``<WORK>``.

    Generators embed the work dir both via ``Path.as_posix()`` and via
    ``str(path)`` (and sometimes after ``resolve()``), so all four spellings
    are substituted.  Longer (resolved) forms are replaced first so a prefix
    match cannot leave a resolved suffix behind.
    """
    wd = Path(work_dir)
    variants = {wd.resolve().as_posix(), wd.as_posix(), str(wd.resolve()), str(wd)}
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, WORK_PLACEHOLDER)
    return text


def assert_matches_golden(rel_path: str, actual_text: str) -> None:
    """Compare *actual_text* against the checked-in golden at *rel_path*.

    *rel_path* is relative to ``tests/golden/expected/`` (e.g.
    ``"yosys/opensta_script.tcl"``).  The text is normalized to end with a
    single trailing newline so checked-in goldens stay POSIX-friendly.

    Behavior (see module docstring for rationale):

    * ``BOOLEY_REGEN_GOLDEN=1`` → (re)write the golden, then FAIL with a
      "regenerated" message so a clean verification run is forced.
    * missing golden → FAIL with the regen hint.
    * mismatch → FAIL with a unified diff and the regen hint.
    """
    if not actual_text.endswith("\n"):
        actual_text += "\n"

    # Leak guard: a tmp path in the output would make the golden machine- and
    # run-specific. Fail loudly and point at the fix.
    leak = _TMP_LEAK_RE.search(actual_text)
    if leak:
        pytest.fail(
            f"golden candidate for {rel_path!r} leaks a temp path "
            f"(matched {leak.group(0)!r}); normalize it with "
            "normalize_work_dir() / use canned fake input paths before "
            "calling assert_matches_golden()."
        )

    golden_path = EXPECTED_DIR / rel_path

    if os.environ.get(REGEN_ENV):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual_text, encoding="utf-8")
        pytest.fail(f"golden regenerated: {golden_path} — re-run without {REGEN_ENV} to verify.")

    if not golden_path.exists():
        pytest.fail(
            f"golden file missing: {golden_path}; generate it with "
            f"{REGEN_ENV}=1, then re-run without the env var to verify."
        )

    expected_text = golden_path.read_text(encoding="utf-8")
    if actual_text != expected_text:
        diff = "\n".join(
            difflib.unified_diff(
                expected_text.splitlines(),
                actual_text.splitlines(),
                fromfile=f"expected/{rel_path}",
                tofile="actual",
                lineterm="",
            )
        )
        pytest.fail(
            f"generated output drifted from golden {rel_path!r}:\n{diff}\n\n"
            f"If the change is intentional, regenerate with {REGEN_ENV}=1 "
            "and re-run to verify (then review the golden diff in git)."
        )
