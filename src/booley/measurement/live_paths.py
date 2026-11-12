"""Find inputs of an exported checkout that read the live tree or the Main Booley Directory.

A checkout run measures a commit, so no Flow input may come from a place that holds
uncommitted work. Two checks find the ways an input can still point there:

* :func:`input_findings` reads the checkout without running anything: committed
  symlinks into a live root, and absolute paths in ``.core`` files, ``fusesoc.conf``
  and the Booley configuration.
* :func:`resolved_findings` judges what FuseSoC resolved for the Target: every file
  and core file inside a live root, and the text of core files generated outside the
  checkout.

The text match is deliberate: it needs no parser per file format, so a live path in a
comment is refused too, and the finding names the file. A candidate path stops at
whitespace and at the other token delimiters (``_STARTERS`` and ``_STOPS`` below).

What the match misses:

* a live root spelled with a delimiter (a space, ``+``, ``=``, ``,``, ``#``, a quote or a
  bracket) is matched by its own literal spelling at a token start, so only another
  spelling of such a root (a second mount) is missed;
* a multi-letter option glued to its path (``-isystem/x``) is not a token start.

Relative escapes (``../../live/x``) are deliberate reads and out of scope for the static
scan; :func:`resolved_findings` still refuses one that reaches a resolved file.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir import in_checkout_booley_dir
from booley.runtime import booley_dir

# Characters after which a path token may start: whitespace, quotes, and the separators of
# shell words (``a&&/x``, ``@/x.f``), TOML/YAML values, ``+incdir+/x``, ``--opt=/x`` and URLs.
_STARTERS = r"""\s"'`<>|;,({\[=:+#&@"""
# A token also ends at the closers of a placeholder (``${VAR}/work``, ``$(pwd)/work``,
# ``%OUT%/work``) and at an escape (``\n``), but one does not start after them: the text
# that follows is relative to the placeholder. NUL ends a token too (a binary file).
_STOPS = _STARTERS + r"""\)}\]\\!%\x00"""
# A candidate starts a token with ``/``, with ``-`` or a one-letter option glued to ``/``
# (``-I/x``), after the default operator of a variable (``${V:-/x}``, ``${V-/x}``) or after
# an undecoded ``\n``/``\r``/``\t`` escape. A relative ``sim/work/tb.sv`` is one token that
# does not start with ``/``.
_TOKEN_START = rf"(?:^|(?<=[{_STARTERS}])|(?<=\\[nrt]))(?:\$\{{\w+:?[-=+?])?(?:-[A-Za-z]?)?"
_CANDIDATE = re.compile(rf"{_TOKEN_START}(/[^{_STOPS}]*)", re.MULTILINE)


@dataclass(frozen=True)
class LiveRoot:
    """A directory that holds uncommitted work, with the name an error gives it."""

    label: str
    path: Path


def _inside(path: Path, roots: Iterable[LiveRoot]) -> tuple[LiveRoot, Path] | None:
    """The first root that contains *path* (symlinks and ``..`` resolved) and its resolved path."""
    for root in roots:
        found = booley_dir.contains(path, booley_dir=root.path)
        if found is not None:
            return root, found
    return None


def _candidates(text: str, roots: Sequence[LiveRoot]) -> list[str]:
    """The absolute-path candidates of *text* in order: tokens, and each root's own spelling.

    A root spelled with a delimiter (``/srv/my proj``, ``/a+b``) is cut by the token
    rules, so each root's literal spelling is matched at a token start too.
    """
    found = {match.start(1): match.group(1) for match in _CANDIDATE.finditer(text)}
    for root in roots:
        # The root must end at a delimiter: ``/workspace`` is not inside ``/work``.
        literal = re.compile(
            rf"{_TOKEN_START}({re.escape(str(root.path))}(?:/[^{_STOPS}]*)?)(?![^{_STOPS}])",
            re.MULTILINE,
        )
        found.update({match.start(1): match.group(1) for match in literal.finditer(text)})
    return [found[start] for start in sorted(found)]


def _text_findings(name: str, text: str, roots: Sequence[LiveRoot]) -> list[str]:
    """One finding per absolute path in *text* that lies inside a live root."""
    findings: list[str] = []
    for candidate in dict.fromkeys(_candidates(text, roots)):
        hit = _inside(Path(candidate), roots)
        if hit is not None:
            root, _ = hit
            findings.append(f"{name} names {candidate}, inside the {root.label} {root.path}")
    return findings


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def input_findings(checkout: Path, roots: Sequence[LiveRoot]) -> list[str]:
    """Findings for committed symlinks and authored files of *checkout* that reach a live root."""
    configuration = [checkout / "booley.toml", checkout / "fusesoc.conf"]
    config_dir = in_checkout_booley_dir(checkout)
    if config_dir.is_dir():
        configuration.extend(sorted(config_dir.glob("*.toml")))
    findings: list[str] = []
    # Read through links: a configuration file or directory linked inside the checkout is
    # still read by the Flow, and the walk below does not follow links.
    for path in configuration:
        if path.is_file():
            findings.extend(
                _text_findings(path.relative_to(checkout).as_posix(), _read(path), roots)
            )
    for directory, subdirectories, files in os.walk(checkout, followlinks=False):
        here = Path(directory)
        file_names = set(files)
        subdirectories.sort()  # os.walk descends in this order: findings stay deterministic
        for name in sorted((*subdirectories, *files)):
            path = here / name
            relative = path.relative_to(checkout).as_posix()
            hit = _inside(path, roots) if path.is_symlink() else None
            if hit is not None:
                root, _ = hit
                findings.append(
                    f"{relative} links to {path.readlink()}, inside the {root.label} {root.path}"
                )
            elif name.endswith(".core") and name in file_names and path.is_file():
                findings.extend(_text_findings(relative, _read(path), roots))
    return findings


def resolved_findings(
    target: str,
    files: Sequence[str],
    cores: Sequence[str],
    checkout: Path,
    roots: Sequence[LiveRoot],
) -> list[str]:
    """Findings for the Target's resolved *files* and *cores* (absolute, lexical paths).

    Only the EDAM's ``files`` (with their ``include_path``) and ``cores`` are judged; the
    sources of other sections (``vpi``) are not.

    A path inside a live root is a finding. A core file outside the checkout was written
    by a generator or comes from a library, so its text is scanned like an authored one;
    cores inside the checkout were scanned by :func:`input_findings`.
    """
    findings: list[str] = []
    core_set = set(cores)
    for entry in dict.fromkeys((*files, *cores)):
        path = Path(entry)
        hit = _inside(path, roots)
        if hit is not None:
            root, resolved = hit
            findings.append(
                f"Target {target} resolves {entry} to {resolved}, inside the {root.label} {root.path}"
            )
        elif entry in core_set and not path.resolve().is_relative_to(checkout) and path.is_file():
            findings.extend(_text_findings(entry, _read(path), roots))
    return findings
