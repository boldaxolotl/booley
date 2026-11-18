"""Keep changed-line coverage strict when code behavior changes."""

import runpy
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / ".github/scripts/terminology_diff_exclusions.py"
MODULE = runpy.run_path(str(SCRIPT))
terminology_only = MODULE["terminology_only"]


def test_renamed_diagnostic_without_code_change_is_excluded() -> None:
    before = '''def check(value):
    """Check the Session Runtime."""
    if not value:
        raise ValueError("Session Runtime unavailable")
    return value
'''
    after = before.replace("Session Runtime", "Sandbox")
    assert terminology_only(before, after)


def test_changed_branch_is_covered_even_with_renamed_diagnostic() -> None:
    before = 'def check(value):\n    if value:\n        raise ValueError("Session Runtime")\n'
    after = 'def check(value):\n    if not value:\n        raise ValueError("Sandbox")\n'
    assert not terminology_only(before, after)


def test_persisted_path_change_is_covered() -> None:
    before = 'PATH = ".runtime/session"\n'
    after = 'PATH = ".sandbox/session"\n'
    assert not terminology_only(before, after)


def test_unrelated_text_change_is_covered() -> None:
    before = 'MESSAGE = "Session Runtime unavailable; retry"\n'
    after = 'MESSAGE = "Sandbox unavailable; ignore the failure"\n'
    assert not terminology_only(before, after)


def test_identifier_rename_without_code_change_is_excluded() -> None:
    identifier_rename_only = MODULE["identifier_rename_only"]
    before = '''from pkg import CampaignStore, helper

__all__ = ["CampaignStore", "helper"]


class CampaignPlan:
    """Plan a campaign."""

    def run(self, campaign_id, *, campaign=None):
        local_campaign = CampaignStore(campaign_id=campaign_id)
        return local_campaign.campaign_id, helper(campaign=campaign)
'''
    after = '''from pkg import RecordStore, helper

__all__ = ["RecordStore", "helper"]


class RecordPlan:
    """Plan a campaign."""

    def run(self, record_id, *, record=None):
        local_record = RecordStore(record_id=record_id)
        return local_record.record_id, helper(record=record)
'''
    assert identifier_rename_only(before, after, {})


def test_reexport_rename_resolves_from_defining_file() -> None:
    excluded_paths = MODULE["excluded_paths"]
    defining = ("class CampaignStore:\n    pass\n", "class RecordStore:\n    pass\n")
    reexport = (
        'from .store import CampaignStore\n\n__all__ = ["CampaignStore"]\n',
        'from .store import RecordStore\n\n__all__ = ["RecordStore"]\n',
    )
    both = excluded_paths({"store.py": defining, "__init__.py": reexport})
    assert sorted(both) == ["__init__.py", "store.py"]
    assert excluded_paths({"__init__.py": reexport}) == []


def test_conflicting_cross_file_renames_are_not_resolved() -> None:
    excluded_paths = MODULE["excluded_paths"]
    changes = {
        "a.py": ("class CampaignStore:\n    pass\n", "class RecordStore:\n    pass\n"),
        "b.py": ("class CampaignStore:\n    pass\n", "class RecordCache:\n    pass\n"),
        "__init__.py": (
            "from .a import CampaignStore\n",
            "from .a import RecordStore\n",
        ),
    }
    assert sorted(excluded_paths(changes)) == ["a.py", "b.py"]


def test_non_terminology_identifier_swap_is_covered() -> None:
    identifier_rename_only = MODULE["identifier_rename_only"]
    assert not identifier_rename_only("y = min(x)\n", "y = max(x)\n", {})


def test_renamed_persisted_key_is_covered() -> None:
    identifier_rename_only = MODULE["identifier_rename_only"]
    before = 'def f(campaign_id):\n    return {"campaign_id": campaign_id}\n'
    after = 'def f(record_id):\n    return {"simulation_record_id": record_id}\n'
    assert not identifier_rename_only(before, after, {})


def test_identifier_merge_is_covered() -> None:
    identifier_rename_only = MODULE["identifier_rename_only"]
    before = "def f(a, b):\n    campaign_a = a\n    campaign_b = b\n    return campaign_a + campaign_b\n"
    after = "def f(a, b):\n    record = a\n    record = b\n    return record + record\n"
    assert not identifier_rename_only(before, after, {})


def test_changed_logic_with_identifier_rename_is_covered() -> None:
    identifier_rename_only = MODULE["identifier_rename_only"]
    before = "def f(campaign):\n    if campaign:\n        return 1\n    return 0\n"
    after = "def f(record):\n    if not record:\n        return 1\n    return 0\n"
    assert not identifier_rename_only(before, after, {})


def test_unordered_import_with_unrelated_change_is_covered() -> None:
    identifier_rename_only = MODULE["identifier_rename_only"]
    before = "from pkg import CampaignStore, helper\n"
    after = "from pkg import RecordStore, other\n"
    assert not identifier_rename_only(before, after, {})
    assert not identifier_rename_only(before, "from pkg2 import RecordStore, helper\n", {})
    assert not identifier_rename_only("from pkg import CampaignStore\n", "x = (\n", {})
    assert not identifier_rename_only(
        '__all__ = ["CampaignStore"]\n', '__all__ = ["RecordStore", "x"]\n', {}
    )
    assert not identifier_rename_only("def f(a): pass\n", "def f(a, *, b=None): pass\n", {})
    assert not identifier_rename_only("global_x = 1\n", "global_x = 1\nz = 2\n", {})


def test_identifier_rename_script_lists_renamed_files(tmp_path: Path) -> None:
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    (tmp_path / "src/booley").mkdir(parents=True)
    module = tmp_path / "src/booley/mod.py"
    module.write_bytes(b"def campaign_f():\n    return 1\n")
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "base"], check=True)
    subprocess.run([*git, "checkout", "-qb", "topic"], check=True)
    module.write_bytes(b"def record_f():\n    return 1\n")
    (tmp_path / "src/booley/new.py").write_bytes(b"X = 1\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "rename"], check=True)
    result = subprocess.run(
        ["python3", str(SCRIPT), "--base", "main"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.split() == [str(module.resolve())]
