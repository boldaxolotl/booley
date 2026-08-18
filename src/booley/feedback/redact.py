"""Deterministic redaction of project identifiers from a findings report.

Why this is code and not a prompt: the destination is a **public** GitHub issue,
where a miss cannot be taken back. An agent asserting "I anonymized it" is an
unverifiable claim; a denylist with unit tests is a reviewable one. The contract
this module offers is therefore narrow and honest — *best-effort scrubbing of the
identifiers we can enumerate, plus a diff for the human to read* — never
"anonymized, safe to publish".

What gets scrubbed, in order (longest match first, so a path is consumed before
the bare directory name inside it):

1. Absolute paths — the repo root, the user's home, and any ``/home/<user>``,
   ``/Users/<user>``, or ``C:\\Users\\<user>`` prefix anywhere in the text.
2. Git remotes — the URL and its ``org/repo`` slug.
3. The committer identity from ``git config``.
4. Design identifiers — the project name, ``.core`` VLNV/target/toplevel names —
   mapped to stable ``<module-N>`` placeholders so the report stays readable and
   two mentions of the same module still correlate.
5. Anything the project listed explicitly (``[feedback] redact_extra``, and
   ``[stealth] banned_words`` when a project has overridden it with its own IP
   vocabulary).

What is deliberately *not* scrubbed, because a report without it is unusable:
EDA tool names and versions, Booley's own vocabulary, error text, and Python
tracebacks. Two of those leak real signal — which EDA tools you have licensed,
and any design identifier that appears inside a quoted log line we did not
enumerate — so the preview says so out loud instead of pretending otherwise.
"""

from __future__ import annotations

import logging
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from booley.flow_names import DEFAULT_TARGET_KEY

logger = logging.getLogger(__name__)

#: Identifiers too generic to redact: replacing them would shred unrelated prose
#: ("the core Target", "top-level module") while protecting nothing — every one
#: of these appears in Booley's own vocabulary.
GENERIC_IDENTIFIERS = frozenset(
    {
        "top",
        "core",
        "main",
        "test",
        "tests",
        "tb",
        "dut",
        "sim",
        "rtl",
        "src",
        "cpu",
        "mem",
        "ram",
        "rom",
        "fifo",
        "axi",
        "apb",
        "ahb",
        "wb",
        "wrapper",
        "soc",
        "ip",
        "clk",
        "rst",
        "reset",
        "clock",
        "design",
        "project",
        "work",
        "build",
        "lint",
        "synth",
        "asic",
        "fpga",
        "default",
        "common",
        "util",
        "utils",
        "misc",
        "top_level",
        "toplevel",
        "verilog",
        "vhdl",
    }
)

#: Below this length an identifier is more likely to collide with ordinary words
#: than to protect anything.
MIN_IDENTIFIER_LEN = 4

_HOME_PATTERNS = (
    re.compile(r"/home/[^/\s:;,\"'`)\]}]+"),
    re.compile(r"/Users/[^/\s:;,\"'`)\]}]+"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\s:;,\"'`)\]}]+", re.IGNORECASE),
)
_EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


@dataclass
class RedactionPlan:
    """The concrete substitutions to apply, plus what they map to.

    ``literals`` are normally matched as plain substrings (URLs and identities),
    while keys named by ``path_literals`` require path-token boundaries.
    ``identifiers`` match on word boundaries (module names, which must not
    match inside a longer name).
    """

    literals: dict[str, str] = field(default_factory=dict)
    #: Literal keys that represent filesystem paths and require token boundaries.
    path_literals: set[str] = field(default_factory=set)
    identifiers: dict[str, str] = field(default_factory=dict)
    #: Regex rules applied after the literals, for shapes we can't enumerate.
    patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list)

    def mapping(self) -> dict[str, str]:
        """Everything being replaced → its placeholder, for the local preview.

        Never write this to anything leaving the machine: it is precisely the
        secret-to-placeholder key.
        """
        return {**self.literals, **self.identifiers}

    def is_empty(self) -> bool:
        return not (self.literals or self.identifiers or self.patterns)


def _git(args: list[str], cwd: Path) -> str:
    """Run a read-only git command, returning stripped stdout ('' on failure)."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.debug("git %s failed: %s", " ".join(args), e)
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _load_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("Could not read %s: %s", path, e)
        return {}


def _core_identifiers(project_root: Path) -> set[str]:
    """Design names worth redacting, scraped from the project's ``.core`` files.

    Deliberately textual rather than a YAML parse: FuseSoC ``.core`` files carry
    tag-bearing YAML that a plain loader chokes on, and all we need are the
    names — a scrape that misses one is a redaction gap the preview will show,
    while a parse error would be a hard failure on the privacy path.
    """
    names: set[str] = set()
    name_re = re.compile(r"^\s*(?:name|toplevel)\s*:\s*(.+?)\s*$", re.MULTILINE)
    for core in sorted(project_root.rglob("*.core")):
        # Skip anything under a Booley state or VCS dir — those hold copies, and
        # walking them doubles the work on a big repo for no new names.
        if any(part in {".git", ".booley_project", ".runtime"} for part in core.parts):
            continue
        try:
            text = core.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for raw in name_re.findall(text):
            # A VLNV ("vendor:library:name:version") contributes each segment;
            # a bare toplevel contributes itself.
            names.update(
                stripped for piece in raw.strip("\"'").split(":") if (stripped := piece.strip())
            )
        names.add(core.stem)
    return names


def _config_identifiers(booley_toml: dict) -> set[str]:
    """Project name and every configured Target name from ``booley.toml``."""
    names: set[str] = set()
    project = booley_toml.get("project")
    if isinstance(project, dict) and isinstance(project.get("name"), str):
        names.add(project["name"])
    flows = booley_toml.get("flows")
    if isinstance(flows, dict):
        for value in flows.values():
            if isinstance(value, dict) and isinstance(value.get(DEFAULT_TARGET_KEY), str):
                # Targets may be VLNV-qualified ("vendor:lib:core#target_name").
                names.update(part for part in re.split(r"[#:]", value[DEFAULT_TARGET_KEY]) if part)
    return names


def _extra_terms(booley_toml: dict) -> set[str]:
    """Terms the project asked for explicitly.

    ``[stealth] banned_words`` counts only when the project overrode it: the
    shipped default exists to keep agent and EDA-tool names out of commit messages and
    includes "booley" itself, which would gut a Booley bug report.
    """
    terms: set[str] = set()
    feedback = booley_toml.get("feedback")
    if isinstance(feedback, dict):
        extra = feedback.get("redact_extra")
        if isinstance(extra, list):
            terms.update(str(t).strip() for t in extra if str(t).strip())
    stealth = booley_toml.get("stealth")
    if isinstance(stealth, dict) and isinstance(stealth.get("banned_words"), list):
        terms.update(str(t).strip() for t in stealth["banned_words"] if str(t).strip())
    return terms


def redact_identifiers_enabled(booley_toml: dict) -> bool:
    """``[feedback] redact_identifiers`` — default **on**.

    Off is a legitimate choice for an open-source project whose module names are
    already public; it makes for a far more actionable issue.
    """
    feedback = booley_toml.get("feedback")
    if isinstance(feedback, dict) and "redact_identifiers" in feedback:
        return bool(feedback["redact_identifiers"])
    return True


def build_plan(project_root: Path, project_dir: Path | None = None) -> RedactionPlan:
    """Assemble the substitutions for *project_root*.

    Args:
        project_root: The RTL repo root — the thing whose identity is at stake.
        project_dir: Booley's state dir, holding ``booley.toml``. Defaults to
            ``<project_root>/.booley_project``.
    """
    project_dir = project_dir or (project_root / ".booley_project")
    booley_toml = _load_toml(project_dir / "booley.toml")
    plan = RedactionPlan()

    # 1. Absolute paths. Longest first is handled at apply time; register both
    #    the resolved and the as-given form, which differ under symlinks.
    for path in {project_root, project_root.resolve()}:
        literal = str(path)
        plan.literals[literal] = "<repo>"
        plan.path_literals.add(literal)
    home = Path.home()
    if home != project_root and str(home) not in plan.literals:
        literal = str(home)
        plan.literals[literal] = "<home>"
        plan.path_literals.add(literal)
    plan.patterns.extend((pattern, r"<home>") for pattern in _HOME_PATTERNS)

    # 2. Git remotes — URL and slug. The slug alone identifies the project on
    #    GitHub, so stripping only the URL would leak it anyway.
    for line in _git(["remote", "-v"], project_root).splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        url = parts[1]
        plan.literals[url] = "<remote>"
        slug = re.sub(r"\.git$", "", url.split(":")[-1].rstrip("/"))
        slug = "/".join(slug.split("/")[-2:])
        if slug.count("/") == 1 and len(slug) >= MIN_IDENTIFIER_LEN:
            plan.literals[slug] = "<org>/<repo>"

    # 3. Committer identity.
    for key, placeholder in (("user.name", "<author>"), ("user.email", "<author-email>")):
        value = _git(["config", "--get", key], project_root)
        if value:
            plan.literals[value] = placeholder
    # Any other address in a log excerpt is somebody's too.
    plan.patterns.append((_EMAIL_PATTERN, "<email>"))

    # 4. Design identifiers → stable <module-N>.
    if redact_identifiers_enabled(booley_toml):
        candidates = _config_identifiers(booley_toml) | _core_identifiers(project_root)
        candidates.add(project_root.name)
        keep = sorted(
            name
            for name in candidates
            if len(name) >= MIN_IDENTIFIER_LEN and name.lower() not in GENERIC_IDENTIFIERS
        )
        for index, name in enumerate(keep, start=1):
            plan.identifiers[name] = f"<module-{index}>"

    # 5. Explicit project terms. Registered last so an explicit term that is
    #    also a module name keeps the (more informative) module placeholder.
    for term in sorted(_extra_terms(booley_toml)):
        if term not in plan.identifiers and term not in plan.literals:
            plan.identifiers[term] = "<redacted>"

    return plan


def apply_plan(text: str, plan: RedactionPlan) -> tuple[str, dict[str, int]]:
    """Redact *text*, returning the result and a hit count per placeholder.

    Idempotent: the placeholders contain none of the secrets, so a second pass
    finds nothing to do. That matters because the report is re-rendered on every
    setup re-run and must not accumulate ``<<repo>>`` nesting.
    """
    hits: dict[str, int] = {}

    def bump(placeholder: str, n: int) -> None:
        if n:
            hits[placeholder] = hits.get(placeholder, 0) + n

    # Literals longest-first: '/home/u/proj' must win over '/home/u'.
    for secret in sorted(plan.literals, key=len, reverse=True):
        placeholder = plan.literals[secret]
        if secret in plan.path_literals:
            pattern = _path_literal_pattern(secret)
            text, count = pattern.subn(
                lambda _match, replacement=placeholder: replacement,
                text,
            )
        else:
            count = text.count(secret)
            if count:
                text = text.replace(secret, placeholder)
        bump(placeholder, count)

    # Identifiers on word boundaries, longest-first so 'foo_top' is consumed
    # before 'foo'.
    for secret in sorted(plan.identifiers, key=len, reverse=True):
        placeholder = plan.identifiers[secret]
        pattern = re.compile(r"\b" + re.escape(secret) + r"\b", re.IGNORECASE)
        text, count = pattern.subn(placeholder, text)
        bump(placeholder, count)

    for pattern, placeholder in plan.patterns:
        text, count = pattern.subn(placeholder, text)
        bump(placeholder, count)

    return text, hits


def _path_literal_pattern(secret: str) -> re.Pattern[str]:
    """Match a path only as a complete token or at a child-path boundary."""
    leading = r"(?<![A-Za-z0-9_.-])"
    trailing = r"(?=$|[/\\\s:;,!?\"'`)\]}.])"
    return re.compile(leading + re.escape(secret) + trailing)


def redact(
    text: str, project_root: Path, project_dir: Path | None = None
) -> tuple[str, dict[str, int]]:
    """Convenience wrapper: build the plan for *project_root* and apply it."""
    return apply_plan(text, build_plan(project_root, project_dir))


def residual_risks(text: str, plan: RedactionPlan) -> list[str]:
    """Leak surfaces a denylist structurally cannot close, for the preview.

    Named explicitly because the honest version of this feature is "here is what
    I could not check for you", not a green tick. Each entry is only listed when
    the text actually shows the shape, so the warning block stays short enough
    to be read.
    """
    risks: list[str] = []
    if re.search(r"\b\d+(\.\d+)?\s?(MHz|GHz|kGE|GE|ns|ps|mW)\b", text):
        risks.append(
            "Performance/area numbers (Fmax, gate count, timing slack) are kept — "
            "they can be competitively sensitive."
        )
        # (Kept because a synthesis bug is unreportable without them.)
    if re.search(
        r"\b(xcelium|vcs|questa|modelsim|design.?compiler|vivado|quartus|innovus)\b", text, re.I
    ):
        risks.append(
            "Commercial EDA tool names are kept — which EDA tools you license is itself "
            "a business signal."
        )
    if re.search(r"^\s*[|+]?\s*\w+\s*(\[|<=|=>|<<)", text, re.M):
        risks.append(
            "Quoted log/RTL excerpts may contain signal or module names that were "
            "never in the .core and so could not be enumerated."
        )
    if "**Attached:**" in text:
        # Attachments are the highest-risk content in the report by some margin:
        # curated findings were written to be shown, a run log was not. Say so
        # whenever one is present, not only when a heuristic happens to fire.
        risks.append(
            "This report inlines an attached file. Log excerpts are arbitrary text: "
            "identifiers that appear only inside them — never in your .core or "
            "booley.toml — could not be enumerated and survive verbatim. Read them below."
        )
    if not plan.identifiers:
        risks.append(
            "Identifier redaction found nothing to map (or is disabled) — design "
            "names appear verbatim."
        )
    risks.append(
        "Redaction is a denylist, not a proof. Read the report below before "
        "agreeing to publish it."
    )
    return risks


def diff_summary(hits: dict[str, int]) -> str:
    """One-line 'what changed' for the consent prompt."""
    if not hits:
        return "no substitutions made"
    return ", ".join(f"{placeholder} (x{count})" for placeholder, count in sorted(hits.items()))
