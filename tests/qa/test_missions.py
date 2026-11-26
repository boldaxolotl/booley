"""Keep QA missions and skills pointing at files that exist."""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from booley.measurement.metrics_file import parse_metrics_as_written
from booley.runtime.paths import skills_dir
from booley.runtime.qa_skill_selection import QA_SKILL_NAMES

QA_ROOT = Path(__file__).resolve().parents[2] / "qa"
MISSIONS = sorted(QA_ROOT.glob("missions/*/MISSION.md"))

# Backticked mission-relative paths such as `fixtures/stealth.md` or
# `../../shared/coverage/RUNBOOK.md`; commands and globs are skipped.
ASSET_PREFIXES = (
    "fixtures/",
    "prompts/",
    "spec/",
    "evaluator/",
    "probes/",
    "../../shared/",
)
BACKTICKED = re.compile(r"`([^`\s]+)`")
MARKDOWN_LINK = re.compile(r"\]\(([^)#\s]+)(?:#[^)]*)?\)")


# QA text must not start a Developer Agent through the retired `booley chat`, or through
# bare `booley`, which launched it. The Codespaces runbook was replaced by
# `shared/t3/RUNBOOK.md`.
UNSUPPORTED_AGENT_LAUNCH = re.compile(
    r"booley chat\b"
    r"|session enter -- booley`"
    r"|\bstart `booley`"
    r"|bare `booley`"
    r"|shared/codespaces/"
)
# Frozen third-party text that QA only quotes.
QA_TEXT_EXEMPT = ("missions/uart/spec/",)


def _asset_references(text: str) -> set[str]:
    """Return backticked asset paths without glob or placeholder characters."""
    return {
        token.rstrip("/")
        for token in BACKTICKED.findall(text)
        if token.startswith(ASSET_PREFIXES) and not any(ch in token for ch in "*<>{}$")
    }


def test_every_mission_is_present():
    assert {path.parent.name for path in MISSIONS} == {
        "coverage",
        "gui",
        "mutation",
        "picorv32",
        "scheduler",
        "taxi",
        "uart",
    }


@pytest.mark.parametrize("mission", MISSIONS, ids=lambda path: path.parent.name)
def test_mission_asset_references_exist(mission: Path):
    text = mission.read_text()
    links = {link for link in MARKDOWN_LINK.findall(text) if "://" not in link}
    missing = sorted(
        ref for ref in _asset_references(text) | links if not (mission.parent / ref).exists()
    )
    assert not missing, f"{mission.relative_to(QA_ROOT)} references missing assets: {missing}"


@pytest.mark.parametrize(
    "document",
    [*sorted(QA_ROOT.glob("*/SKILL.md")), *sorted(QA_ROOT.glob("*.md"))],
    ids=lambda path: str(path.relative_to(QA_ROOT)),
)
def test_relative_links_resolve(document: Path):
    links = {link for link in MARKDOWN_LINK.findall(document.read_text()) if "://" not in link}
    missing = sorted(link for link in links if not (document.parent / link).exists())
    assert not missing, f"{document.relative_to(QA_ROOT)} links to missing files: {missing}"


@pytest.mark.parametrize("skill", sorted(QA_ROOT.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_installable_skill_markdown_links_do_not_escape_skill_directory(skill: Path):
    links = {link for link in MARKDOWN_LINK.findall(skill.read_text()) if "://" not in link}
    escaping = sorted(link for link in links if ".." in Path(link).parts)
    assert not escaping, f"{skill.relative_to(QA_ROOT)} has escaping links: {escaping}"


def test_uart_review_contract_contains_frozen_spec_text() -> None:
    spec = QA_ROOT / "missions/uart/spec"
    contract = (spec / "contract.md").read_text(encoding="utf-8")
    assert (spec / "corpus/hw/ip/uart/doc/registers.md").read_text(encoding="utf-8") in contract
    assert (spec / "mmio-addendum.md").read_text(encoding="utf-8") in contract
    assert len(contract) <= 30_000


def test_taxi_setup_prompt_carries_the_sdc_fixture() -> None:
    """The verbatim setup prompt hands the Setup agent the fixture's exact SDC text."""
    taxi = QA_ROOT / "missions/taxi"
    prompt = (taxi / "prompts/setup.md").read_text(encoding="utf-8")
    sdc = (taxi / "fixtures/taxi_mac_10g.sdc").read_text(encoding="utf-8")
    assert f"```tcl\n{sdc}```\n" in prompt


def test_capability_map_links_resolve_to_mission_areas() -> None:
    document = QA_ROOT / "AREAS.md"
    links = re.findall(
        r"\[([^]]+)\]\(([^)]+)\)( \(optional\))?", document.read_text(encoding="utf-8")
    )
    assert links
    for label, link, marker in links:
        assert "#" not in link, link
        mission = document.parent / link
        assert mission.is_file(), link
        headings = dict(
            re.findall(
                r"^### \d+\. ([\w-]+) —(.*)$", mission.read_text(encoding="utf-8"), re.MULTILINE
            )
        )
        area = label.split("/", 1)[1]
        assert area in headings, label
        # The map marks exactly the areas the mission schedules outside its primary timebox.
        assert bool(marker) == ("optional follow-up" in headings[area]), label


def test_qa_skill_names_do_not_overlap_packaged_product_skills() -> None:
    assert all((QA_ROOT / name / "SKILL.md").is_file() for name in QA_SKILL_NAMES)
    assert QA_SKILL_NAMES.isdisjoint(path.name for path in skills_dir().iterdir())


def test_scheduler_history_helper_round_trips(tmp_path: Path) -> None:
    """`history_edit.py` rewrites matching records and restores the original bytes."""
    helper = QA_ROOT / "missions" / "scheduler" / "fixtures" / "history_edit.py"
    history = tmp_path / "history.json"
    record = {
        "key": {"target": "sim_icarus", "phase": "mem_hog", "flags": [], "test_run": ""},
        "peak_memory_mb": 300.0,
        "wall_s": 9.0,
        "finished_at": "2026-11-14T00:00:00Z",
    }
    other = {**record, "key": {**record["key"], "phase": "sim_build"}}
    original = json.dumps(
        {"$schema": "booley.job-resource-history/v1", "records": [record, other]}
    )
    history.write_text(original, encoding="utf-8")

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", str(helper), *args, str(history)],
            capture_output=True,
            text=True,
            check=False,
        )

    assert run("set", "--phase", "mem_hog", "--memory-mb", "40", "--wall-s", "2").returncode == 0
    edited = json.loads(history.read_text(encoding="utf-8"))["records"]
    assert [(r["peak_memory_mb"], r["wall_s"]) for r in edited] == [(40.0, 2.0), (300.0, 9.0)]
    assert run("set", "--phase", "missing", "--wall-s", "1").returncode == 1
    assert "mem_hog" in run("show").stdout
    assert run("restore").returncode == 0
    assert history.read_text(encoding="utf-8") == original


def test_metrics_fixtures_load_with_the_product_parser() -> None:
    fixtures = sorted(QA_ROOT.rglob("*metrics.toml"))
    assert fixtures, "QA ships no metrics.toml fixture"
    for fixture in fixtures:
        parse_metrics_as_written(fixture.read_bytes())


def _qa_text_files() -> list[Path]:
    """Every Markdown and YAML file under qa/ except frozen third-party text."""
    files = [*QA_ROOT.rglob("*.md"), *QA_ROOT.rglob("*.yaml")]
    return sorted(
        path
        for path in files
        if not path.relative_to(QA_ROOT).as_posix().startswith(QA_TEXT_EXEMPT)
    )


def test_unsupported_agent_launch_pattern_matches_launch_forms_only() -> None:
    launches = [
        "booley session enter -- booley chat --cli codex",
        "Launch it with `booley session enter -- booley`, or",
        "In one terminal start `booley` and send",
        "bare `booley` starts the configured provider",
        "../../shared/codespaces/RUNBOOK.md",
    ]
    allowed = [
        "`booley auth --cli codex`",
        "run `booley session enter -- codex` (or `claude`)",
        "A bare `claude` or `codex` session is not a supported workflow",
        "`codex` and `claude` each get Booley's MCP tools",
        "with `git worktree add` and run `booley worktree prepare` in it",
        "../../shared/t3/RUNBOOK.md",
    ]
    assert all(UNSUPPORTED_AGENT_LAUNCH.search(text) for text in launches)
    assert not any(UNSUPPORTED_AGENT_LAUNCH.search(text) for text in allowed)


def test_qa_text_never_starts_developer_agents_through_retired_launchers() -> None:
    offenders = [
        f"{path.relative_to(QA_ROOT)}:{number}: {line.strip()}"
        for path in _qa_text_files()
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if UNSUPPORTED_AGENT_LAUNCH.search(line)
    ]
    assert not offenders, "QA text starts an agent through a retired launcher:\n" + "\n".join(
        offenders
    )
