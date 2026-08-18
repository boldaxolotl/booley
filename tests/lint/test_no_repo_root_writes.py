"""Architecture guard: no tool may default an OUTPUT-location arg to cwd.

Bug class (recent fixes c78725e, 2088efe, 59ede2e): generated / scratch state
lands in the git repo root because a tool's output-location argparse argument
*defaults to ``Path.cwd()``*. When the flag is omitted, the tool then writes
its artifacts under whatever directory it was launched from — often the repo
root.

This test AST-scans every ``.py`` under ``src/booley/dev_support/`` and FAILS if an
argparse argument whose name implies an OUTPUT location (``--output-dir``,
``--out-dir``, ``--report-dir``) defaults to ``Path.cwd()`` /
``pathlib.Path.cwd()`` / ``os.getcwd()``. The fix is to leave the default
``None`` and resolve lazily under the worktree ``--work-dir`` or the project
helper (``booley.project_dir.resolve_project_dir`` / ``runtime_dir``).

``--work-dir`` itself is deliberately EXCLUDED: it is the worktree root and
defaulting it to cwd is the conventional CLI behaviour for standalone/human
runs (the developer always passes it explicitly). The repo-root-pollution
bugs came from *output* paths not derived from that work dir, not from the
work dir default.

Conservative by design: only args with an output-location name AND a cwd
default are flagged. Args with no default, or already resolved via the helper,
are ignored. If a legitimate exception ever surfaces, add its ``(file, arg)``
pair to ``_ALLOWLIST``.
"""

from __future__ import annotations

import ast
from pathlib import Path

# Flag spellings that imply an OUTPUT location (--work-dir excluded; see module
# docstring — the worktree root may default to cwd for standalone runs).
_WRITE_FLAGS = {"--output-dir", "--out-dir", "--report-dir"}
# dest tokens (a dest containing any of these implies an output location).
_WRITE_DEST_TOKENS = ("output_dir", "out_dir", "report_dir")

# Known-legitimate exceptions: (relative posix path under src/, arg name). Empty
# by design — add a pair here only with a comment justifying why cwd is correct.
_ALLOWLIST: set[tuple[str, str]] = set()

_DEV_SUPPORT_DIR = Path(__file__).resolve().parents[2] / "src" / "booley" / "dev_support"


def _flag_to_dest(flag: str) -> str:
    """Mirror argparse: ``--work-dir`` -> ``work_dir``."""
    return flag.lstrip("-").replace("-", "_")


def _arg_names(call: ast.Call) -> tuple[list[str], str | None]:
    """Return (flag strings, explicit dest) for an ``add_argument`` call."""
    flags = [
        a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)
    ]
    dest: str | None = None
    for kw in call.keywords:
        if (
            kw.arg == "dest"
            and isinstance(kw.value, ast.Constant)
            and isinstance(kw.value.value, str)
        ):
            dest = kw.value.value
    return flags, dest


def _is_write_location_arg(flags: list[str], dest: str | None) -> bool:
    """True when the arg's name implies a write/work location."""
    if any(f in _WRITE_FLAGS for f in flags):
        return True
    # Resolve every candidate dest (explicit dest + derived from each flag).
    dests = [dest] if dest else []
    dests += [_flag_to_dest(f) for f in flags if f.startswith("--")]
    return any(d and any(tok in d for tok in _WRITE_DEST_TOKENS) for d in dests)


def _is_cwd_default(node: ast.AST | None) -> bool:
    """True when *node* is ``Path.cwd()`` / ``pathlib.Path.cwd()`` / ``os.getcwd()``."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    # `Path.cwd()`, `pathlib.Path.cwd()`, `os.getcwd()` -> attribute call.
    if isinstance(func, ast.Attribute):
        return func.attr in {"cwd", "getcwd"}
    # bare `getcwd()` (from-imported).
    if isinstance(func, ast.Name):
        return func.id == "getcwd"
    return False


def _default_kwarg(call: ast.Call) -> ast.AST | None:
    for kw in call.keywords:
        if kw.arg == "default":
            return kw.value
    return None


def _display_name(flags: list[str], dest: str | None) -> str:
    return flags[0] if flags else (dest or "<unknown>")


def _find_violations(py_file: Path) -> list[str]:
    rel = py_file.relative_to(_DEV_SUPPORT_DIR.parents[2]).as_posix()
    try:
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    except (SyntaxError, OSError) as e:  # pragma: no cover - unparsable source
        return [f"{rel}: could not parse ({e})"]

    violations: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            continue
        flags, dest = _arg_names(node)
        if not _is_write_location_arg(flags, dest):
            continue
        if not _is_cwd_default(_default_kwarg(node)):
            continue
        arg = _display_name(flags, dest)
        if (rel, arg) in _ALLOWLIST or (py_file.name, arg) in _ALLOWLIST:
            continue
        violations.append(
            f"{rel}: argparse arg {arg!r} defaults to cwd (Path.cwd()/os.getcwd()). "
            "This makes an omitted flag write output into the launch directory "
            "(often the repo root). Leave default=None and resolve lazily under "
            "the worktree --work-dir or booley.project_dir.runtime_dir() instead."
        )
    return violations


def test_no_developer_utility_defaults_write_dir_to_cwd() -> None:
    """No src/booley/dev_support/*.py argparse write-location arg defaults to cwd."""
    all_violations: list[str] = []
    for py_file in sorted(_DEV_SUPPORT_DIR.rglob("*.py")):
        all_violations.extend(_find_violations(py_file))
    assert not all_violations, "\n".join(all_violations)
