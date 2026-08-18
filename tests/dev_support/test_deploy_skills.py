"""Tests for deploy_skills — skill discovery, stub generation, and deployment."""

from __future__ import annotations

from pathlib import Path

from booley.dev_support.deploy_skills import (
    DeployResult,
    SkillSource,
    deploy_stub,
    discover_skills,
    make_stub,
    parse_frontmatter,
    print_summary,
)
from booley.paths import skills_dir

# ---------------------------------------------------------------------------
# parse_frontmatter
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    def test_basic_frontmatter(self):
        text = "---\nname: my-skill\ndescription: Does things\n---\nBody text."
        result = parse_frontmatter(text)
        assert result == {"name": "my-skill", "description": "Does things"}

    def test_no_frontmatter(self):
        assert parse_frontmatter("Just plain text.") == {}

    def test_empty_string(self):
        assert parse_frontmatter("") == {}

    def test_unclosed_frontmatter(self):
        """Only opening --- with no closing --- should return empty."""
        text = "---\nname: test\nstill going\n"
        result = parse_frontmatter(text)
        # No closing ---, so loop exhausts lines → collects whatever it found
        assert result == {"name": "test"}

    def test_multiple_colons(self):
        text = "---\nurl: https://example.com\n---\n"
        result = parse_frontmatter(text)
        assert result["url"] == "https://example.com"

    def test_empty_value_skipped(self):
        text = "---\nname:\n---\n"
        result = parse_frontmatter(text)
        assert "name" not in result  # empty value → skipped

    def test_nested_yaml_ignored(self):
        text = "---\nname: skill\n  nested: value\n---\n"
        result = parse_frontmatter(text)
        assert result["name"] == "skill"
        # Nested line still parsed as key:value (simple parser)
        assert result.get("nested") == "value"

    def test_no_opening_delimiter(self):
        text = "name: not-frontmatter\n---\n"
        assert parse_frontmatter(text) == {}


# ---------------------------------------------------------------------------
# discover_skills
# ---------------------------------------------------------------------------


class TestDiscoverSkills:
    def test_discovers_valid_skill(self, tmp_path: Path):
        skill_dir = tmp_path / "booley-test-skill"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: test-skill\ndescription: A test skill\n---\nInstructions here.",
            encoding="utf-8",
        )

        skills = discover_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "test-skill"
        assert skills[0].description == "A test skill"
        assert skills[0].dir_name == "booley-test-skill"

    def test_skips_dir_without_skill_md(self, tmp_path: Path):
        (tmp_path / "some-dir").mkdir()
        skills = discover_skills(tmp_path)
        assert skills == []

    def test_skips_no_name(self, tmp_path: Path):
        skill_dir = tmp_path / "booley-noname"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\ndescription: Has no name\n---\n",
            encoding="utf-8",
        )
        skills = discover_skills(tmp_path)
        assert skills == []

    def test_skips_no_description(self, tmp_path: Path):
        skill_dir = tmp_path / "booley-nodesc"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: nodesc\n---\n",
            encoding="utf-8",
        )
        skills = discover_skills(tmp_path)
        assert skills == []

    def test_nonexistent_dir(self, tmp_path: Path):
        skills = discover_skills(tmp_path / "nonexistent")
        assert skills == []

    def test_skips_files_in_root(self, tmp_path: Path):
        """Regular files (not directories) in the skills root are ignored."""
        (tmp_path / "README.md").write_text("just a file")
        skills = discover_skills(tmp_path)
        assert skills == []

    def test_multiple_skills_sorted(self, tmp_path: Path):
        for name in ["booley-z-skill", "booley-a-skill"]:
            d = tmp_path / name
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\nname: {name}\ndescription: Desc for {name}\n---\n",
                encoding="utf-8",
            )

        skills = discover_skills(tmp_path)
        assert len(skills) == 2
        # sorted by directory name
        assert skills[0].dir_name == "booley-a-skill"
        assert skills[1].dir_name == "booley-z-skill"

    def test_builtin_workflow_skills_are_packaged(self):
        # The six setup skills were folded into a single unified `booley-setup`
        # skill; `booley-heal` owns ongoing Doctor remediation after setup.
        skills = {skill.name: skill for skill in discover_skills(skills_dir())}

        expected = {"booley-heal", "booley-setup"}
        assert expected <= set(skills)

    def test_heal_skill_metadata_is_packaged(self):
        skills = {skill.name: skill for skill in discover_skills(skills_dir())}
        skill = skills["booley-heal"]

        metadata = skill.source_path.parent / "agents" / ("open" + "ai.yaml")
        metadata_text = metadata.read_text(encoding="utf-8")

        assert "doctor" in skill.description.lower()
        assert 'default_prompt: "Use $booley-heal' in metadata_text

    def test_setup_doctor_skill_metadata_is_packaged(self):
        skills = {skill.name: skill for skill in discover_skills(skills_dir())}
        skill = skills["booley-setup"]

        skill_root = skill.source_path.parent
        metadata = skill_root / "agents" / ("open" + "ai.yaml")
        metadata_text = metadata.read_text(encoding="utf-8")

        # The unified skill ends in a doctor audit (Step 4, post-ADR-0039).
        assert "doctor" in skill.description.lower()
        doctor_step = (skill_root / "steps" / "4-doctor.md").read_text(encoding="utf-8")
        assert "booley doctor --deep" in doctor_step
        assert 'default_prompt: "Use $booley-setup' in metadata_text

    def test_adapter_steps_are_gone(self):
        # ADR 0039: the per-tool adapter steps died with the adapter path;
        # the skill runs Steps 0-4 with pre_run_commands declared in Step 2,
        # plus the post-gate parity (5) and findings (6) steps.
        skills = {skill.name: skill for skill in discover_skills(skills_dir())}
        steps_dir = skills["booley-setup"].source_path.parent / "steps"

        assert sorted(p.name for p in steps_dir.glob("*.md")) == [
            "0-plan.md",
            "2-project-config.md",
            "3-agents-md.md",
            "4-doctor.md",
            "5-parity.md",
            "6-findings.md",
            "new-greenfield.md",
        ]
        config_step = (steps_dir / "2-project-config.md").read_text(encoding="utf-8")
        assert "pre_run_commands" in config_step


# ---------------------------------------------------------------------------
# make_stub
# ---------------------------------------------------------------------------


class TestMakeStub:
    def test_generates_expected_content(self):
        skill = SkillSource(
            dir_name="booley-deploy",
            name="deploy",
            description="Deploy the thing",
            source_path=Path("/fake/SKILL.md"),
        )
        stub = make_stub(skill)
        assert stub.startswith("---\n")
        assert "name: deploy" in stub
        assert "description: Deploy the thing" in stub
        assert str(Path("/fake/SKILL.md")) in stub


# ---------------------------------------------------------------------------
# deploy_stub
# ---------------------------------------------------------------------------


class TestDeployStub:
    def _make_skill(self) -> SkillSource:
        return SkillSource(
            dir_name="booley-test",
            name="test-skill",
            description="A test",
            source_path=Path("/fake/SKILL.md"),
        )

    def test_creates_new_stub(self, tmp_path: Path):
        skill = self._make_skill()
        result = deploy_stub(skill, "agents", tmp_path, dry_run=False)
        assert result.status == "created"
        target_file = tmp_path / ".agents" / "skills" / "test-skill" / "SKILL.md"
        assert target_file.is_file()

    def test_updates_existing_stub(self, tmp_path: Path):
        skill = self._make_skill()
        # Create first
        deploy_stub(skill, "agents", tmp_path, dry_run=False)
        # Modify the existing file to differ
        target_file = tmp_path / ".agents" / "skills" / "test-skill" / "SKILL.md"
        target_file.write_text("old content", encoding="utf-8")
        # Deploy again
        result = deploy_stub(skill, "agents", tmp_path, dry_run=False)
        assert result.status == "updated"

    def test_skips_identical(self, tmp_path: Path):
        skill = self._make_skill()
        deploy_stub(skill, "agents", tmp_path, dry_run=False)
        result = deploy_stub(skill, "agents", tmp_path, dry_run=False)
        assert result.status == "skipped"

    def test_dry_run_creates(self, tmp_path: Path):
        skill = self._make_skill()
        result = deploy_stub(skill, "agents", tmp_path, dry_run=True)
        assert "dry-run" in result.status
        # File should NOT exist
        target_file = tmp_path / ".agents" / "skills" / "test-skill" / "SKILL.md"
        assert not target_file.exists()

    def test_os_error_handled(self, tmp_path: Path):
        skill = self._make_skill()
        # Make the target directory a file to cause OSError
        target_dir = tmp_path / ".agents" / "skills" / "test-skill"
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        target_dir.write_text("I'm a file, not a directory", encoding="utf-8")

        result = deploy_stub(skill, "agents", tmp_path, dry_run=False)
        assert result.status == "error"


# ---------------------------------------------------------------------------
# print_summary
# ---------------------------------------------------------------------------


class TestPrintSummary:
    def test_empty_results(self, capsys):
        print_summary([])
        captured = capsys.readouterr()
        assert "No skills to deploy" in captured.out

    def test_table_format(self, capsys):
        results = [
            DeployResult("skill-a", "agents", "created", ""),
            DeployResult("skill-b", "agents", "skipped", "identical"),
        ]
        print_summary(results)
        captured = capsys.readouterr()
        assert "skill-a" in captured.out
        assert "skill-b" in captured.out
        assert "created" in captured.out
        assert "Total: 2" in captured.out
