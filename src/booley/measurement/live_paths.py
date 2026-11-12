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
whitespace, so a live root whose spelling contains a space is not matched; the
Sandbox roots (``/work``, ``/booley-project``) have none. A multi-letter option glued
to its path (``-isystem/x``) is not a token start and is not matched either.

Relative escapes (``../../live/x``) are deliberate reads and out of scope here.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from booley.core.booley_dir import in_checkout_booley_dir
from booley.runtime import booley_dir

# Characters that end a path token: whitespace, quotes, and the separators of shell
# words, TOML/YAML values, ``+incdir+/x``, ``--opt=/x`` and URLs.
_DELIMITERS = r"""\s"'`<>|;,(){}\[\]=:+#"""
# A candidate starts a token with ``/`` or with a one-letter option glued to ``/``
# (``-I/x``). A relative ``sim/work/tb.sv`` is one token that does not start with ``/``.
_CANDIDATE = re.compile(
    rf"(?:^|(?<=[{_DELIMITERS}]))(?:-[A-Za-z])?(/[^{_DELIMITERS}]*)", re.MULTILINE
)


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


def _text_findings(name: str, text: str, roots: Sequence[LiveRoot]) -> list[str]:
    """One finding per absolute path in *text* that lies inside a live root."""
    findings: list[str] = []
    for match in _CANDIDATE.finditer(text):
        candidate = match.group(1)
        hit = _inside(Path(candidate), roots)
        if hit is not None:
            root = hit[0]
            findings.append(f"{name} names {candidate}, inside the {root.label} {root.path}")
    return findings


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def input_findings(checkout: Path, roots: Sequence[LiveRoot]) -> list[str]:
    """Findings for committed symlinks and authored files of *checkout* that reach a live root."""
    booley_toml = [checkout / "booley.toml", checkout / "fusesoc.conf"]
    config_dir = in_checkout_booley_dir(checkout)
    if config_dir.is_dir():
        booley_toml.extend(sorted(config_dir.glob("*.toml")))
    configuration = {path for path in booley_toml if path.is_file() and not path.is_symlink()}
    findings: list[str] = []
    for directory, subdirectories, files in os.walk(checkout, followlinks=False):
        here = Path(directory)
        for name in sorted((*subdirectories, *files)):
            path = here / name
            relative = path.relative_to(checkout).as_posix()
            if path.is_symlink():
                hit = _inside(path, roots)
                if hit is not None:
                    root = hit[0]
                    target = path.readlink()
                    findings.append(
                        f"{relative} links to {target}, inside the {root.label} {root.path}"
                    )
            elif path in configuration or (name in files and name.endswith(".core")):
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
