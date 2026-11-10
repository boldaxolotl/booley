"""User docs must not offer a bare axis (`sim`, `lint`, `synth`, `fpga`) as a Target name.

Targets are named ``<axis>_<subject>`` (``docs/user/TARGETS.md``); a bare axis
says nothing about the design it drives. Vendored upstream names are the one
exception and are described in prose that these patterns do not match.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_AXES = "sim|lint|synth|fpga"

# Each pattern matches one way a doc can present a bare axis as a usable Target.
_BARE_AXIS_PATTERNS = {
    "backticked axis list": re.compile(rf"Targets?\b[^.()]{{0,60}}\(`(?:{_AXES})`, `(?:{_AXES})`"),
    "--target <axis>": re.compile(rf"--target (?:{_AXES})(?![\w-])"),
    'target="<axis>"': re.compile(rf'target="(?:{_AXES})"'),
    "bare axis is fine": re.compile(rf"[Bb]are `(?:{_AXES})` is fine"),
    "Name the Target <axis> or": re.compile(rf"Name the Target `(?:{_AXES})` or"),
}
# A YAML Target key inside a fenced block: `  sim:` directly under `targets:`.
_YAML_KEY = re.compile(rf"^ {{2}}(?:{_AXES}):(?:\s|$)")


def _doc_paths() -> list[Path]:
    return [*sorted((ROOT / "docs/user").glob("*.md")), ROOT / "docs/internals/FLOW-EXECUTION.md"]


def _paragraphs(text: str) -> list[str]:
    """Paragraphs with whitespace collapsed, so a phrase wrapped across lines still matches."""
    return [" ".join(block.split()) for block in re.split(r"\n\s*\n", text)]


def _yaml_target_keys(text: str) -> list[str]:
    hits: list[str] = []
    in_targets = False
    for line in text.splitlines():
        if line.startswith("targets:"):
            in_targets = True
        elif line and not line.startswith(" "):
            in_targets = False
        if in_targets and _YAML_KEY.match(line):
            hits.append(line.strip())
    return hits


def test_user_docs_offer_no_bare_axis_target() -> None:
    offenders: list[str] = []
    for path in _doc_paths():
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(ROOT).as_posix()
        for paragraph in _paragraphs(text):
            offenders += [
                f"{name}: {label}: {paragraph[:80]}"
                for label, pattern in _BARE_AXIS_PATTERNS.items()
                if pattern.search(paragraph)
            ]
        offenders += [f"{name}: YAML target key: {key}" for key in _yaml_target_keys(text)]
    assert not offenders, "\n".join(offenders)


def test_detector_catches_each_bare_axis_form() -> None:
    """Guard the guard: every pattern fires on a known-bad snippet."""
    samples = {
        "backticked axis list": "one Target per enabled flow (`sim`, `lint`, and `synth`)",
        "--target <axis>": "run it with --target lint now",
        'target="<axis>"': 'LintRequest(target="lint", work_dir=x)',
        "bare axis is fine": "A bare `lint` is fine here.",
        "Name the Target <axis> or": "Name the Target `fpga` or `fpga_<subject>`.",
    }
    assert samples.keys() == _BARE_AXIS_PATTERNS.keys()
    for label, sample in samples.items():
        assert _BARE_AXIS_PATTERNS[label].search(sample), label
    assert not _BARE_AXIS_PATTERNS["backticked axis list"].search(
        "A Booley Flow (`sim`, `lint`, `synth`, `fpga`) looks the same"
    )
    assert _yaml_target_keys("targets:\n  sim:\n    flow: sim\n") == ["sim:"]
    assert _yaml_target_keys("targets:\n  sim_dut:\n    flow: sim\n") == []
    assert not _BARE_AXIS_PATTERNS["--target <axis>"].search("--target lint_soc")
