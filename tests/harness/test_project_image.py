"""Tests for the project sandbox image builder (ADR 0018)."""

from __future__ import annotations

import subprocess

import pytest

from booley.runtime import project_image as pi

# ===========================================================================
# project_image_name
# ===========================================================================


class TestImageName:
    def test_slug_from_dir(self, tmp_path):
        d = tmp_path / "myproj"
        d.mkdir()
        assert pi.project_image_name(d) == "myproj-booley-sandbox"

    def test_sanitizes(self, tmp_path):
        d = tmp_path / "My Proj!"
        d.mkdir()
        assert pi.project_image_name(d) == "my-proj-booley-sandbox"


# ===========================================================================
# resolve_requirements — only [sandbox].pip_requirements is baked
# ===========================================================================


class TestResolve:
    def test_listed_files_used_and_missing_reported(self, tmp_path):
        (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
        existing, missing = pi.resolve_requirements(tmp_path, ["a.txt", "nope.txt"])
        assert [p.name for p in existing] == ["a.txt"]
        assert missing == ["nope.txt"]

    def test_none_bakes_nothing(self, tmp_path):
        # No auto-discovery: a root requirements.txt is ignored unless listed.
        (tmp_path / "requirements.txt").write_text("root-pkg==1.0\n", encoding="utf-8")
        assert pi.resolve_requirements(tmp_path, None) == ([], [])

    def test_empty_list_bakes_nothing(self, tmp_path):
        (tmp_path / "requirements.txt").write_text("root-pkg==1.0\n", encoding="utf-8")
        assert pi.resolve_requirements(tmp_path, []) == ([], [])


# ===========================================================================
# consolidation (dedup + provenance)
# ===========================================================================


class TestConsolidate:
    def test_dedup_and_provenance(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("# comment\nalpha-pkg==1.2.3\nshared\n", encoding="utf-8")
        b = tmp_path / "b.txt"
        b.write_text("beta-pkg\nshared\n", encoding="utf-8")
        body, kept, skipped, dropped = pi.consolidated_requirements(tmp_path, [a, b])
        assert "alpha-pkg==1.2.3" in body
        assert "beta-pkg" in body
        assert body.count("shared") == 1  # deduped
        assert "# Sources: a.txt, b.txt" in body
        assert "# from a.txt" in body and "# from b.txt" in body
        assert set(kept) == {"alpha-pkg==1.2.3", "shared", "beta-pkg"}
        assert skipped == []
        assert dropped == []

    def test_skips_local_editable_and_file_reqs(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text(
            "alpha-pkg==1.2.3\n"
            "localpkg @ file:../ext/localpkg\n"
            "-e ./local-pkg\n"
            "./another\n"
            "-r other.txt\n"
            "git+https://github.com/x/y.git@v1\n",  # remote VCS is bakeable
            encoding="utf-8",
        )
        body, kept, skipped, _ = pi.consolidated_requirements(tmp_path, [a])
        assert "alpha-pkg==1.2.3" in kept
        assert "git+https://github.com/x/y.git@v1" in kept
        assert "localpkg @ file:../ext/localpkg" in skipped
        assert "-e ./local-pkg" in skipped
        assert "./another" in skipped
        assert "-r other.txt" in skipped
        for s in skipped:
            assert s not in body

    def test_all_local_yields_no_kept(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("pkg @ file:../sub\n-e ./x\n", encoding="utf-8")
        _, kept, skipped, _ = pi.consolidated_requirements(tmp_path, [a])
        assert kept == []
        assert len(skipped) == 2

    def test_drops_managed_package_pins(self, tmp_path):
        # SETUP-5: a design repo's fusesoc/edalize pin would shadow the image's
        # managed version — drop it (don't bake) and report it.
        a = tmp_path / "a.txt"
        a.write_text(
            "alpha-pkg==1.2.3\n"
            "fusesoc==2.4.3\n"  # downgrades the managed 2.4.6
            "edalize>=0.6\n"
            "FuseSoC == 2.4.5\n"  # case/spacing variant, still managed
            "edalize-plugin==1.0\n",  # NOT managed (different project)
            encoding="utf-8",
        )
        body, kept, _skipped, dropped = pi.consolidated_requirements(tmp_path, [a])
        assert kept == ["alpha-pkg==1.2.3", "edalize-plugin==1.0"]
        assert dropped == ["fusesoc==2.4.3", "edalize>=0.6", "FuseSoC == 2.4.5"]
        for d in dropped:
            assert d not in body
        assert "edalize-plugin==1.0" in body

    def test_managed_pin_via_at_url_and_extras(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text(
            "fusesoc[extra]==2.4.3\nbooley @ https://example/booley.whl\nkeepme==1\n",
            encoding="utf-8",
        )
        _, kept, _, dropped = pi.consolidated_requirements(tmp_path, [a])
        assert kept == ["keepme==1"]
        assert dropped == ["fusesoc[extra]==2.4.3", "booley @ https://example/booley.whl"]


# ===========================================================================
# build_project_image (mocked docker)
# ===========================================================================


class TestBuild:
    def test_build_invokes_docker_with_context(self, tmp_path, monkeypatch):
        docker_dir = tmp_path / "docker"
        docker_dir.mkdir()
        (docker_dir / "Dockerfile").write_text("FROM booley-sandbox\n", encoding="utf-8")
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(pi.subprocess, "run", fake_run)
        assert pi.build_project_image("img", docker_dir) is True
        assert calls[0][:3] == ["docker", "build", "-t"]
        assert str(docker_dir) in calls[0]  # context is the small docker dir

    def test_build_missing_dockerfile(self, tmp_path):
        assert pi.build_project_image("img", tmp_path / "nope") is False


class TestManagedFileGuard:
    def test_absent_is_managed(self, tmp_path):
        assert pi.is_managed_generated_file(tmp_path / "nope") is True

    def test_generated_header_is_managed(self, tmp_path):
        docker_dir = tmp_path / "docker"
        body, _, _, _ = pi.consolidated_requirements(tmp_path, [])
        pi.write_project_image_files(docker_dir, body)
        assert pi.is_managed_generated_file(docker_dir / "Dockerfile") is True
        assert pi.is_managed_generated_file(docker_dir / "requirements.txt") is True

    def test_hand_written_is_user_owned(self, tmp_path):
        f = tmp_path / "Dockerfile"
        f.write_text("FROM booley-sandbox\nRUN apt-get install -y gcc\n", encoding="utf-8")
        assert pi.is_managed_generated_file(f) is False

    def test_edited_generated_file_is_user_owned(self, tmp_path):
        # SETUP-6 hole: an edit that KEEPS the generated header used to read
        # as managed and was clobbered on re-init; the stamped self-hash now
        # flags any edit as user-owned.
        docker_dir = tmp_path / "docker"
        body, _, _, _ = pi.consolidated_requirements(tmp_path, [])
        pi.write_project_image_files(docker_dir, body)
        df = docker_dir / "Dockerfile"
        df.write_text(df.read_text(encoding="utf-8") + "RUN echo mine\n", encoding="utf-8")
        assert pi.is_managed_generated_file(df) is False

    def test_legacy_generated_file_without_hash_stays_managed(self, tmp_path):
        # Files written before hash stamping carry only the header — they must
        # keep regenerating (legacy behavior), not freeze as user-owned.
        f = tmp_path / "Dockerfile"
        f.write_text(f"{pi._GENERATED_HEADER}\nFROM booley-sandbox\n", encoding="utf-8")
        assert pi.is_managed_generated_file(f) is True

    def test_keep_directive_is_user_owned(self, tmp_path):
        # Still has the generated header but flagged hands-off.
        docker_dir = tmp_path / "docker"
        body, _, _, _ = pi.consolidated_requirements(tmp_path, [])
        pi.write_project_image_files(docker_dir, body)
        df = docker_dir / "Dockerfile"
        df.write_text(df.read_text() + "# booley:keep\nRUN echo mine\n", encoding="utf-8")
        assert pi.is_managed_generated_file(df) is False


class TestDockerfile:
    @pytest.mark.parametrize(
        ("from_line", "expected"),
        [
            ("FROM booley-sandbox-riscv", "booley-sandbox-riscv"),
            ("FROM --platform=linux/amd64 booley-sandbox:latest AS runtime", "booley-sandbox"),
            ("FROM ${BASE_IMAGE}", None),
        ],
    )
    def test_parent_image_resolution(self, tmp_path, from_line, expected):
        dockerfile = tmp_path / "Dockerfile"
        dockerfile.write_text(from_line + "\n", encoding="utf-8")

        assert pi.dockerfile_parent_image(dockerfile) == expected

    def test_installs_as_root_into_system(self, tmp_path):
        docker_dir = tmp_path / "docker"
        pi.consolidated_requirements(tmp_path, [])
        pi.write_project_image_files(docker_dir, "alpha-pkg==1.2.3\n")
        body = (docker_dir / "Dockerfile").read_text(encoding="utf-8")
        assert "FROM booley-sandbox" in body
        assert "USER root" in body
        assert "USER agent" in body
        # System install (not --user) so the pip-local volume can't shadow it.
        assert "--user" not in body

    def test_write_managed_dockerfile_writes_only_the_dockerfile(self, tmp_path):
        """F-5 backfill: a hand-authored requirements.txt gets the managed
        Dockerfile beside it — and nothing else — still stamped as managed so
        a later re-init may regenerate it."""
        docker_dir = tmp_path / "docker"
        hand_reqs = "# mine\ncocotb==1.9.2\n"
        docker_dir.mkdir()
        (docker_dir / "requirements.txt").write_text(hand_reqs, encoding="utf-8")

        dockerfile = pi.write_managed_dockerfile(docker_dir)

        assert dockerfile == docker_dir / "Dockerfile"
        assert pi.is_managed_generated_file(dockerfile)
        assert (docker_dir / "requirements.txt").read_text(encoding="utf-8") == hand_reqs
        assert sorted(p.name for p in docker_dir.iterdir()) == ["Dockerfile", "requirements.txt"]


# ===========================================================================
# Curated-stack overrides (F-13)
# ===========================================================================


BASE_VERSIONS = {"cocotb": "2.0.1", "cocotbext-axi": "0.1.28", "pytest": "8.0.0"}


class TestBaseImagePackages:
    def _run(self, monkeypatch, *, stdout: str = "", returncode: int = 0, exc=None):
        def _fake(cmd, **kwargs):
            if exc is not None:
                raise exc
            return subprocess.CompletedProcess(cmd, returncode, stdout, "")

        monkeypatch.setattr(pi.subprocess, "run", _fake)

    def test_parses_pip_freeze_and_normalizes_names(self, monkeypatch):
        self._run(monkeypatch, stdout="cocotb==2.0.1\ncocotb_ext.AXI==0.1.28\nnoversion\n")

        assert pi.base_image_packages() == {"cocotb": "2.0.1", "cocotb-ext-axi": "0.1.28"}

    def test_unqueryable_image_is_not_an_error(self, monkeypatch):
        """Advisory by contract: no base image means nothing to advise about."""
        self._run(monkeypatch, returncode=125)
        assert pi.base_image_packages() == {}

        self._run(monkeypatch, exc=FileNotFoundError("docker"))
        assert pi.base_image_packages() == {}


class TestCuratedOverrides:
    def test_downgrade_of_a_curated_package_is_named(self):
        """The report case: a project pin quietly replaced the base image's
        validated cocotb, and the build said nothing."""
        overrides = pi.curated_overrides(["cocotb==1.5.1", "cocotbext-axi==0.1.10"], BASE_VERSIONS)

        assert overrides == [
            ("cocotb==1.5.1", "cocotb", "2.0.1"),
            ("cocotbext-axi==0.1.10", "cocotbext-axi", "0.1.28"),
        ]

    def test_matching_pin_and_bare_name_are_silent(self):
        """Neither can move the installed version, so neither is news."""
        assert pi.curated_overrides(["cocotb==2.0.1", "pytest"], BASE_VERSIONS) == []

    def test_package_absent_from_the_base_is_silent(self):
        assert pi.curated_overrides(["cocotb-bus==0.2.1"], BASE_VERSIONS) == []

    def test_ranges_and_extras_and_markers_are_reported(self):
        """A range needs a resolver to evaluate, so it is reported, not guessed."""
        overrides = pi.curated_overrides(
            ["cocotb[bus] <2.0 ; python_version >= '3.11'"], BASE_VERSIONS
        )

        assert [name for _, name, _ in overrides] == ["cocotb"]


class TestRequirementName:
    def test_forms(self):
        assert pi.requirement_name("cocotb==1.5.1") == "cocotb"
        assert pi.requirement_name("cocotb_ext.AXI[all]>=0.1") == "cocotb-ext-axi"
        assert pi.requirement_name("pkg @ https://example.com/pkg.whl") == "pkg"
        assert pi.requirement_name("-r other.txt") is None
        assert pi.requirement_name("https://example.com/pkg.whl") is None
