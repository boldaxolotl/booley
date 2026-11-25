"""Keep changed-line coverage strict when code behavior changes."""

import runpy
import sys
from pathlib import Path

import pytest
from tests.git_support import git, repository
from tests.subprocess_budget import run_bounded_child

SCRIPT = Path(__file__).parents[2] / ".github/scripts/terminology_diff_exclusions.py"
MODULE = runpy.run_path(str(SCRIPT))
terminology_only = MODULE["terminology_only"]
identifier_rename_only = MODULE["identifier_rename_only"]
excluded_paths = MODULE["excluded_paths"]


_DIAGNOSTIC = 'def check(value):\n    """Check the Session Runtime."""\n    raise ValueError("Session Runtime unavailable")\n'
_PLAN = '''from pkg import CampaignStore, helper

__all__ = ["CampaignStore", "helper"]


class CampaignPlan:
    """Plan a campaign."""

    def run(self, campaign_id, *, campaign=None):
        local_campaign = CampaignStore(campaign_id=campaign_id)
        return local_campaign.campaign_id, helper(campaign=campaign)
'''
_RENAMED = (
    _PLAN.replace("CampaignStore", "RecordStore")
    .replace("CampaignPlan", "RecordPlan")
    .replace("campaign_id", "record_id")
    .replace("campaign=", "record=")
    .replace("campaign)", "record)")
    .replace("local_campaign", "local_record")
)


@pytest.mark.parametrize(
    "before, after, excluded",
    [
        (_DIAGNOSTIC, _DIAGNOSTIC.replace("Session Runtime", "Sandbox"), True),
        (
            'def check(value):\n    if value:\n        raise ValueError("Session Runtime")\n',
            'def check(value):\n    if not value:\n        raise ValueError("Sandbox")\n',
            False,
        ),
        ('PATH = ".runtime/session"\n', 'PATH = ".sandbox/session"\n', False),
        (
            'M = "Session Runtime unavailable; retry"\n',
            'M = "Sandbox unavailable; ignore it"\n',
            False,
        ),
    ],
)
def test_terminology_only(before: str, after: str, excluded: bool) -> None:
    assert terminology_only(before, after) is excluded


@pytest.mark.parametrize(
    "before, after, excluded",
    [
        (_PLAN, _RENAMED, True),
        ("y = min(x)\n", "y = max(x)\n", False),
        (
            'def f(campaign_id):\n    return {"campaign_id": campaign_id}\n',
            'def f(record_id):\n    return {"simulation_record_id": record_id}\n',
            False,
        ),
        (
            "def f(a, b):\n    campaign_a = a\n    campaign_b = b\n",
            "def f(a, b):\n    record = a\n    record = b\n",
            False,
        ),
        (
            "def f(campaign):\n    if campaign:\n        return 1\n",
            "def f(record):\n    if not record:\n        return 1\n",
            False,
        ),
        ("from pkg import CampaignStore, helper\n", "from pkg import RecordStore, other\n", False),
        (
            "from pkg import CampaignStore, helper\n",
            "from pkg2 import RecordStore, helper\n",
            False,
        ),
        ("from pkg import CampaignStore\n", "x = (\n", False),
        ('__all__ = ["CampaignStore"]\n', '__all__ = ["RecordStore", "x"]\n', False),
        ("def f(a): pass\n", "def f(a, *, b=None): pass\n", False),
        ("global_x = 1\n", "global_x = 1\nz = 2\n", False),
    ],
)
def test_identifier_rename_only(before: str, after: str, excluded: bool) -> None:
    assert identifier_rename_only(before, after, {}) is excluded


def test_reexport_rename_resolves_from_defining_file() -> None:
    defining = ("class CampaignStore:\n    pass\n", "class RecordStore:\n    pass\n")
    reexport = (
        'from .store import CampaignStore\n\n__all__ = ["CampaignStore"]\n',
        'from .store import RecordStore\n\n__all__ = ["RecordStore"]\n',
    )
    both = excluded_paths({"store.py": defining, "__init__.py": reexport})
    assert sorted(both) == ["__init__.py", "store.py"]
    assert excluded_paths({"__init__.py": reexport}) == []


def test_conflicting_cross_file_renames_are_not_resolved() -> None:
    changes = {
        "a.py": ("class CampaignStore:\n    pass\n", "class RecordStore:\n    pass\n"),
        "b.py": ("class CampaignStore:\n    pass\n", "class RecordCache:\n    pass\n"),
        "__init__.py": ("from .a import CampaignStore\n", "from .a import RecordStore\n"),
    }
    assert sorted(excluded_paths(changes)) == ["a.py", "b.py"]


def test_identifier_rename_script_lists_renamed_files(tmp_path: Path) -> None:
    repo = repository(tmp_path / "repo")
    (repo / "src/booley").mkdir(parents=True)
    module = repo / "src/booley/mod.py"
    module.write_bytes(b"def campaign_f():\n    return 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-qb", "topic")
    module.write_bytes(b"def record_f():\n    return 1\n")
    (repo / "src/booley/new.py").write_bytes(b"X = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "rename")
    result = run_bounded_child(
        [sys.executable, str(SCRIPT), "--base", "main"], cwd=repo, check=True
    )
    assert result.stdout.split() == [str(module.resolve())]
