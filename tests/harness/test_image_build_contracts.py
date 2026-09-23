"""Distribution-safe Sandbox Image compatibility contracts."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from booley.runtime import image_build_contracts as contracts
from booley.runtime.build_stamp import BuildProfile, write_build_stamp
from booley.runtime.version_attribution import VersionAttribution, VersionOrigin

SOURCE_ROOT = Path(__file__).resolve().parents[2]


def _source_tree(root: Path) -> Path:
    docker = root / "src" / "booley" / "data" / "docker"
    docker.mkdir(parents=True)
    (docker / "stable-base-inputs.txt").write_text("base.txt\n", encoding="utf-8")
    (root / "base.txt").write_text("base\n", encoding="utf-8")
    edalize = root / "src" / "booley" / "data" / "edalize" / "verible.py"
    edalize.parent.mkdir(parents=True)
    edalize.write_text("adapter\n", encoding="utf-8")
    bwave = root / "crates" / "bwave"
    for relative in ("Cargo.toml", "Cargo.lock", "src/lib.rs", "schema/q.json", "docs/a.md"):
        path = bwave / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(relative + "\n", encoding="utf-8")
    return root


def test_source_contracts_are_deterministic_and_independently_scoped(tmp_path: Path) -> None:
    root = _source_tree(tmp_path)
    initial = contracts.source_image_build_contracts(root)
    (root / "crates" / "bwave" / "src" / "lib.rs").write_text("changed\n", encoding="utf-8")
    changed = contracts.source_image_build_contracts(root)

    assert changed.runtime_base == initial.runtime_base
    assert changed.standard_substrate != initial.standard_substrate
    assert len(initial.runtime_base) == len(initial.standard_substrate) == 64


def test_source_contract_rejects_missing_and_symlinked_inputs(tmp_path: Path) -> None:
    root = _source_tree(tmp_path)
    (root / "crates" / "bwave" / "Cargo.lock").unlink()
    with pytest.raises(ValueError, match="missing or unsafe"):
        contracts.standard_substrate_contract(root)

    target = root / "lock-target"
    target.write_text("lock\n", encoding="utf-8")
    (root / "crates" / "bwave" / "Cargo.lock").symlink_to(target)
    with pytest.raises(ValueError, match="missing or unsafe"):
        contracts.standard_substrate_contract(root)


@pytest.mark.parametrize(
    "runtime,standard",
    [
        ("a" * 63, "b" * 64),
        ("A" * 64, "b" * 64),
        ("a" * 64, "computed"),
        ("a" * 64, None),
    ],
)
def test_embedded_contracts_reject_noncanonical_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    standard: str | None,
) -> None:
    stamp = tmp_path / "_build_commit.py"
    lines = [f"RUNTIME_BASE_CONTRACT = {runtime!r}"]
    if standard is not None:
        lines.append(f"STANDARD_SUBSTRATE_CONTRACT = {standard!r}")
    stamp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(contracts, "_embedded_stamp_path", lambda: stamp)

    with pytest.raises(contracts.ImageBuildContractMetadataError):
        contracts.embedded_image_build_contracts()


def test_embedded_contracts_reject_dynamic_expressions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stamp = tmp_path / "_build_commit.py"
    stamp.write_text(
        "RUNTIME_BASE_CONTRACT = 'a' * 64\nSTANDARD_SUBSTRATE_CONTRACT = 'b' * 64\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(contracts, "_embedded_stamp_path", lambda: stamp)

    with pytest.raises(contracts.ImageBuildContractMetadataError, match="invalid"):
        contracts.embedded_image_build_contracts()


def test_origin_resolver_uses_only_attributed_source_root(tmp_path: Path) -> None:
    root = _source_tree(tmp_path / "source")
    attribution = VersionAttribution("1.0.0", VersionOrigin.SOURCE, source_root=root)

    assert contracts.expected_image_build_contracts(attribution) == (
        contracts.source_image_build_contracts(root)
    )


def test_publisher_and_build_stamp_share_standard_contract_calculator() -> None:
    workflow = (SOURCE_ROOT / ".github" / "workflows" / "docker-publish.yml").read_text(
        encoding="utf-8"
    )
    stamp = (SOURCE_ROOT / "src" / "booley" / "runtime" / "build_stamp.py").read_text(
        encoding="utf-8"
    )
    owner = (SOURCE_ROOT / "src" / "booley" / "runtime" / "image_build_contracts.py").read_text(
        encoding="utf-8"
    )
    test_workflow = (SOURCE_ROOT / ".github" / "workflows" / "test.yml").read_text(
        encoding="utf-8"
    )

    assert "image_build_contracts import standard_substrate_contract" in workflow
    assert workflow.count("io.booley.runtime-base.contract=") >= 2
    assert workflow.count("io.booley.standard-substrate.contract=") >= 2
    assert "--expected-runtime-base-contract" in workflow
    assert "--expected-standard-substrate-contract" in workflow
    assert "source_image_build_contracts(booley_root)" in stamp
    assert "standard_substrate_contract(root)" in owner
    assert "profile=BuildProfile.DEVELOPMENT_WHEEL" in test_workflow


def test_installed_wheel_plans_and_prepares_hybrid_graph_without_checkout_access(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    shutil.copytree(
        SOURCE_ROOT,
        checkout,
        ignore=shutil.ignore_patterns(
            ".git", ".worktrees", ".runtime", ".venv", "target", "build", "dist"
        ),
    )
    expected = contracts.source_image_build_contracts(checkout)
    write_build_stamp(checkout, profile=BuildProfile.OFFICIAL_RELEASE)
    wheel_dir = tmp_path / "wheel"
    build_python = sys.executable
    built = subprocess.run(
        [
            build_python,
            "-m",
            "build",
            "--wheel",
            "--outdir",
            str(wheel_dir),
        ],
        cwd=checkout,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    wheel = next(wheel_dir.glob("booley_rtl-*.whl"))
    installed = tmp_path / "site-packages"
    with zipfile.ZipFile(wheel) as archive:
        archive.extractall(installed)
    project = tmp_path / "project"
    config = project / ".booley_project" / "booley.toml"
    config.parent.mkdir(parents=True)
    (project / "requirements.txt").write_text("cocotb==2.0.1\n", encoding="utf-8")
    config.write_text(
        '[sandbox]\npip_requirements = ["requirements.txt"]\n',
        encoding="utf-8",
    )
    driver = tmp_path / "driver.py"
    driver.write_text(_INSTALLED_WHEEL_DRIVER, encoding="utf-8")
    environment = os.environ.copy()
    for name in tuple(environment):
        if name.startswith("COV_CORE_") or name in {"COVERAGE_FILE", "COVERAGE_PROCESS_START"}:
            environment.pop(name)
    environment.update(
        {
            "PYTHONNOUSERSITE": "1",
            "PYTHONPATH": str(installed),
        }
    )
    completed = subprocess.run(
        [build_python, "-P", str(driver), str(project), str(SOURCE_ROOT)],
        cwd=tmp_path,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    observed = json.loads(completed.stdout)

    assert observed["origin"] == "distribution"
    assert observed["contracts"] == [expected.runtime_base, expected.standard_substrate]
    assert observed["references"] == ["booley-sandbox", "project-booley-sandbox"]
    assert observed["roles"] == ["wheel-overlay", "project-overlay"]
    assert observed["actions"] == ["pull", "build"]
    assert observed["prepared"] == 2


_INSTALLED_WHEEL_DRIVER = r"""\
import json
import sys
from pathlib import Path

forbidden = Path(sys.argv[2]).resolve()
def audit(event, args):
    if event != "open" or not args or not isinstance(args[0], (str, bytes)):
        return
    path = Path(args[0]).resolve()
    if path.is_relative_to(forbidden):
        raise RuntimeError(f"forbidden checkout read: {path}")
sys.addaudithook(audit)

import booley
from booley.harness import image_lifecycle as harness_lifecycle
from booley.runtime import image_lifecycle as lifecycle
from booley.runtime.image_build_contracts import expected_image_build_contracts

class Docker:
    def __init__(self): self.images = {}
    def image_id(self, ref): return self.images.get(ref, (None, {}))[0]
    def label(self, ref, name): return self.images.get(ref, (None, {}))[1].get(name)
    def repo_digests(self, ref): return ()
    def image_references(self): return ()
    def container_image_ids(self): return frozenset()
    def tag(self, source, target): self.images[target] = self.images[source]
    def remove_tag(self, ref): self.images.pop(ref, None)

class Builder:
    def __init__(self, docker): self.docker = docker
    def prepare(self, node, *, candidate_reference, parent_reference):
        labels = dict(node.expected_labels)
        if node.acquisition_policy is lifecycle.ArtifactPolicy.VERIFIED_RELEASE_ONLY:
            labels.update({
                lifecycle.LABEL_BUILD_ORIGIN: "registry",
                lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_REGISTRY_DIGEST,
                lifecycle.LABEL_PARENT_ARTIFACT: "ghcr.io/boldaxolotl/base@sha256:" + "d" * 64,
                lifecycle.LABEL_WHEEL_SHA256: "e" * 64,
            })
        else:
            labels.update({
                lifecycle.LABEL_BUILD_ORIGIN: "local",
                lifecycle.LABEL_PARENT_ARTIFACT_KIND: lifecycle.PARENT_ARTIFACT_LOCAL_IMAGE_ID,
                lifecycle.LABEL_PARENT_ARTIFACT: self.docker.image_id(parent_reference),
            })
        self.docker.images[candidate_reference] = (
            "sha256:" + f"{len(self.docker.images) + 1:064x}", labels
        )
        return candidate_reference

docker = Docker()
harness_lifecycle._docker_adapter = lambda: docker
plan = harness_lifecycle.plan(lifecycle.ProjectImageScope(Path(sys.argv[1])))
prepared = lifecycle.prepare(plan, docker=docker, builder=Builder(docker))
image_contracts = expected_image_build_contracts(booley.version_attribution)
print(json.dumps({
    "origin": booley.version_attribution.origin.value,
    "contracts": [image_contracts.runtime_base, image_contracts.standard_substrate],
    "references": [node.reference for node in plan.nodes],
    "roles": [node.role.value for node in plan.nodes],
    "actions": [step.action.value for step in plan.steps],
    "prepared": len(prepared.candidates),
}))
"""
