"""Static contracts for the shipped Booley sandbox image."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import yaml
from tests.sidecar_image_helpers import DIND_IMAGE

_DOCKERFILE = Path("src/booley/data/docker/Dockerfile")
_DOCKER_DIR = _DOCKERFILE.parent
_BASE_DOCKERFILE = _DOCKER_DIR / "Dockerfile.base"


def test_claude_sdk_cli_duplicate_is_removed_in_install_layer() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    install_start = dockerfile.index("RUN python -m ensurepip --default-pip")
    install_end = dockerfile.index("\n\n# EDA invocation", install_start)
    install_layer = dockerfile[install_start:install_end]

    assert "CLAUDE_SDK_BUNDLED_CLI=" in install_layer
    assert 'rm -f "$CLAUDE_SDK_BUNDLED_CLI"' in install_layer
    assert 'test ! -e "$CLAUDE_SDK_BUNDLED_CLI"' in install_layer
    assert "python -m pip check" in install_layer
    assert "ClaudeSDKBackend" not in install_layer


def test_all_python_installs_tolerate_slow_publisher_reads() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    install_start = dockerfile.index("RUN python -m ensurepip --default-pip")
    install_end = dockerfile.index("\n\n# Publisher transfers", install_start)
    invariant_install = dockerfile[install_start:install_end]

    assert "--timeout 120" in invariant_install
    assert "--retries 10" in invariant_install
    assert "PIP_DEFAULT_TIMEOUT=120" in dockerfile
    assert "PIP_RETRIES=10" in dockerfile


def test_stable_base_owns_invariant_runtime_and_candidate_owns_application() -> None:
    """The published boundary keeps ordinary source edits out of EDA layers."""
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    candidate = _DOCKERFILE.read_text(encoding="utf-8")
    pyproject_copy = base.index("COPY pyproject.toml")
    dependency_exporter = base.index("COPY src/booley/data/docker/export_project_dependencies.py")
    project_install = base.index("--requirement /tmp/booley-build/project-dependencies.txt")
    image_dependencies = base.index('"cocotb==2.1.0"')

    assert pyproject_copy < dependency_exporter < project_install
    assert project_install < image_dependencies
    assert "YOSYS_REF" in base
    assert (
        "FROM docker.io/openroad/ubuntu24.04@sha256:"
        "c34542dd5c3624117e8370cfb3a4f37a40bfce73a25f5cefdad3277c4c46ce8a"
    ) in base
    assert (
        "FROM docker.io/openroad/ubuntu24.04-dev@sha256:"
        "1cfdeba85a28a0bd2a4fca1a5b357fa7f715838941b87a0eeff1686494b1c1db "
        "AS eda-artifacts"
    ) in base
    assert (
        "FROM docker.io/library/ubuntu:24.04@sha256:"
        "33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517"
    ) in base
    assert "COPY --from=eda-artifacts /usr/local/share/yosys/ /usr/local/share/yosys/" in base
    assert "COPY --from=eda-artifacts /usr/local/lib/ivl/ /usr/local/lib/ivl/" in base
    assert (
        "COPY --from=eda-artifacts /usr/local/share/verilator/ /usr/local/share/verilator/" in base
    )
    assert "COPY --from=openroad-artifacts /opt/or-tools/lib/ /opt/or-tools/lib/" in base
    assert "COPY --from=openroad-artifacts /opt/or-tools/include/" not in base
    assert "ARG VERIBLE_VERSION=v0.0-4163-g6cce8f19" in base
    assert "verible-verilog-lint --version" in base
    assert "COPY dist/booley_rtl-*.whl" not in base
    assert "COPY crates/bwave/" not in base
    assert "COPY src/booley/data/edalize/verible.py" not in base

    assert "FROM booley-runtime-base" in candidate
    assert "--mount=type=bind,source=dist,target=/tmp/booley-dist,readonly" in candidate
    assert "COPY dist/booley_rtl-*.whl" not in candidate
    assert "COPY crates/bwave/" in candidate
    assert "COPY src/booley/data/edalize/verible.py" in candidate
    assert "--no-deps" in candidate
    assert '--wheel "$WHEEL"' in candidate
    assert "ClaudeSDKBackend" not in candidate
    assert 'test -x "$(command -v claude)"' in candidate
    assert 'test "$(claude --version | awk \'{print $1}\')" = "2.1.259"' in candidate
    assert "python -m pip check" in candidate


def test_stable_base_asserts_cocotb_2_1_icarus_library_contract() -> None:
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert 'test "$(cocotb-config --version)" = "2.1.0"' in base
    assert 'test -e "$(cocotb-config --lib-name-path vpi icarus)"' in base
    assert "cocotb-config --lib-name vpi icarus" not in base
    assert "cocotb-config --lib-name-path vpi icarus).vpl" not in base


def test_every_local_docker_copy_source_is_allowed_by_dockerignore() -> None:
    """The whitelist context cannot silently omit a newly introduced COPY."""
    allowed = [
        line[1:].rstrip("/")
        for line in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.startswith("!")
    ]
    local_sources: list[str] = []
    for dockerfile in (_BASE_DOCKERFILE, _DOCKERFILE):
        for line in dockerfile.read_text(encoding="utf-8").splitlines():
            if not line.startswith("COPY ") or "--from=" in line:
                continue
            fields = shlex.split(line)
            local_sources.extend(field.rstrip("/") for field in fields[1:-1])

    assert local_sources
    missing = [
        source
        for source in local_sources
        if not any(source == entry or source.startswith(f"{entry}/") for entry in allowed)
    ]
    assert not missing, f"Docker COPY sources excluded by .dockerignore: {missing}"


def test_verible_patch_does_not_depend_on_importing_candidate_package() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    patch_start = dockerfile.index("COPY src/booley/data/edalize/verible.py")
    wheel_install = dockerfile.index("--mount=type=bind,source=dist")
    patch_region = dockerfile[patch_start:wheel_install]

    assert "/tmp/booley-build/verible.py" in patch_region
    assert "import booley" not in patch_region


def test_read_only_wheel_mount_is_not_mutated() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    mount_start = dockerfile.index("--mount=type=bind,source=dist")
    mount_end = dockerfile.index("# bwave", mount_start)
    mount_region = dockerfile[mount_start:mount_end]

    assert 'rm -f "$WHEEL"' not in mount_region
    assert "rm -f /tmp/booley-installed-files.txt" in mount_region


def test_bwave_runtime_paths_are_created_as_one_layer_hard_links() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    bwave_start = dockerfile.index("# bwave — VCD waveform parser")
    bwave_end = dockerfile.index("ENV BOOLEY_BWAVE_BIN", bwave_start)
    bwave_region = dockerfile[bwave_start:bwave_end]

    assert "RUN --mount=type=bind,from=bwave-builder" in bwave_region
    assert "COPY --from=bwave-builder" not in bwave_region
    assert "install -m 0755 /tmp/bwave/bwave /usr/local/libexec/booley/bwave" in bwave_region
    assert 'ln /usr/local/libexec/booley/bwave "$BWAVE_BIN_DIR/bwave"' in bwave_region


def test_ci_captures_docker_cache_and_layer_evidence() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "docker history --no-trunc" in workflow
    assert "docker image inspect booley-test" in workflow
    assert "docker-build-evidence" in workflow
    assert ".github/scripts/image_contract.py" in workflow
    assert "runtime-contract.json" in workflow


def test_published_runtime_images_include_sbom_attestations() -> None:
    release = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    base_release = Path(".github/workflows/docker-base-publish.yml").read_text(encoding="utf-8")

    assert release.count("sbom: true") == 2
    assert base_release.count("sbom: true") == 1


def test_ci_builds_and_tests_candidate_riscv_image_before_release() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    verifier = Path(".github/scripts/verify_picorv32_demo.sh").read_text(encoding="utf-8")

    assert "--file src/booley/data/docker/Dockerfile.riscv" in workflow
    assert "--build-context booley-sandbox=docker-image://booley-test" in workflow
    assert "--image booley-riscv-test" in workflow
    assert "--base-image booley-test" in workflow
    assert "--flavor riscv" in workflow
    assert "--runtime-image riscv=booley-riscv-test" in workflow
    assert "verify_picorv32_demo.sh" in workflow
    assert "-e BOOLEY_RUN_PICORV32_FLOWS=1" in workflow
    assert "python -m booley.flows.lint --work-dir /work --target lint_core" in verifier
    assert "python -m booley.flows.sim --work-dir /work --target sim_core" in verifier
    assert "riscv-image-evidence-${{ github.run_id }}-${{ github.run_attempt }}" in workflow


def test_runtime_contract_and_setup_agree_that_rust_is_not_installed() -> None:
    contract = Path(".github/contracts/session-runtime.toml").read_text(encoding="utf-8")
    setup = Path("src/booley/data/skills/booley-setup/steps/2-project-config.md").read_text(
        encoding="utf-8"
    )

    assert '"/usr/local/cargo"' in contract
    assert '"/usr/local/rustup"' in contract
    assert "Rust is not included" in setup
    assert "Node.js, Rust" not in setup


def test_readme_uses_current_slim_image_measurements() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "15 GB of Docker storage" not in readme
    assert "21 GB" not in readme
    assert "4 GB of Docker storage" in readme
    assert "6 GB" in readme
    assert "1.58/2.02 GB" in readme
    assert "2.82/4.48 GB" in readme


def test_ci_builds_sidecar_candidates_and_archives_historical_controls() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    evidence_script = Path(".github/scripts/sidecar-build-evidence.sh").read_text(encoding="utf-8")
    archive_path = Path(".github/scripts/archive/sidecar-python313-comparison.sh")
    archive_script = archive_path.read_text(encoding="utf-8")

    assert "bash .github/scripts/sidecar-build-evidence.sh" in workflow
    assert str(archive_path) not in workflow
    assert "docker build --pull --no-cache" in evidence_script
    for dockerfile in (
        "Dockerfile.egress-proxy",
        "Dockerfile.flexnet-relay",
        "Dockerfile.reaper",
    ):
        assert dockerfile in evidence_script
    assert evidence_script.count(":py314") >= 3
    assert ":py313" not in evidence_script
    assert '"Python 3.13.15"' not in evidence_script
    assert '"Python 3.14.7"' in evidence_script
    assert "source-repodigests.tsv" in evidence_script
    assert f'readonly DOCKER_DIND="{DIND_IMAGE}"' in evidence_script
    assert 'capture_source docker-dind "${DOCKER_DIND}"' in evidence_script
    assert (
        'readonly BOOKWORM_CANDIDATE="python:3.14.7-slim-bookworm@sha256:'
        '9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f"' in evidence_script
    )
    assert (
        'readonly ALPINE_CANDIDATE="python:3.14.7-alpine3.24@sha256:'
        'c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc"' in evidence_script
    )
    assert (
        'readonly DOCKER_CLI="docker:29.7.2-cli@sha256:'
        '3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e"' in evidence_script
    )
    assert evidence_script.count("src/booley/eda/provisioning/licensing") == 1
    assert archive_script.count(":py313") >= 3
    assert '"Python 3.13.15"' in archive_script
    assert "BOOLEY_EGRESS_PROXY_IMAGE: booley-egress-proxy:py314" in workflow
    assert "BOOLEY_REAPER_IMAGE: booley-reaper:py314" in workflow
    assert "BOOLEY_FLEXNET_DOCKER_TEST" in workflow
    assert "test_egress_proxy_image_e2e.py" in workflow
    assert "test_reaper_image_e2e.py" in workflow
    assert "test_flexnet_relay_e2e.py" in workflow


def test_shipped_external_base_images_are_digest_pinned() -> None:
    for path in sorted(_DOCKER_DIR.glob("Dockerfile*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("FROM "):
                continue
            image = line.split()[1]
            # The RISC-V flavor deliberately consumes a locally built named
            # context; the release workflow maps that name to the exact digest
            # emitted by the base-image job.
            if image in {"${BOOLEY_BASE_IMAGE}", "booley-runtime-base"}:
                continue
            assert re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image), (
                f"{path}: external base image is not digest-pinned: {image}"
            )


def test_reaper_uses_pinned_runtime_stages_without_live_package_install() -> None:
    reaper = (_DOCKER_DIR / "Dockerfile.reaper").read_text(encoding="utf-8")

    assert (
        "FROM docker:29.7.2-cli@sha256:"
        "3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e"
    ) in reaper
    assert "apk add" not in reaper
    assert "COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker" in reaper


def test_sidecars_pin_python_3_14_7_without_changing_distributions() -> None:
    egress = (_DOCKER_DIR / "Dockerfile.egress-proxy").read_text(encoding="utf-8")
    flexnet = (_DOCKER_DIR / "Dockerfile.flexnet-relay").read_text(encoding="utf-8")
    reaper = (_DOCKER_DIR / "Dockerfile.reaper").read_text(encoding="utf-8")

    assert (
        "FROM python:3.14.7-slim-bookworm@sha256:"
        "9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f" in egress
    )
    alpine = (
        "FROM python:3.14.7-alpine3.24@sha256:"
        "c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc"
    )
    assert alpine in flexnet
    assert alpine in reaper


def test_sandbox_downloads_are_verified_before_use() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")

    assert "| bash" not in dockerfile
    assert "@^" not in dockerfile
    for checksum_arg in (
        "SV2V_SHA256",
        "VERIBLE_SHA256",
        "NODE_SHA256",
    ):
        assert f"ARG {checksum_arg}=" in dockerfile
        assert f"${{{checksum_arg}}}" in dockerfile
    for checksum_arg in (
        "XPACK_GCC_SHA256",
        "ISA_MANUAL_PDF_SHA256",
        "ISA_MANUAL_HTML_SHA256",
        "DEBUG_SPEC_SHA256",
        "PSABI_SHA256",
    ):
        assert f"ARG {checksum_arg}=" in riscv
        assert f"${{{checksum_arg}}}" in riscv

    lock = (_DOCKER_DIR / "agent-clis-package-lock.json").read_text(encoding="utf-8")
    assert '"@anthropic-ai/claude-code": "2.1.259"' in lock
    assert '"@openai/codex": "0.153.1"' in lock
    assert lock.count('"integrity": "sha512-') == 16
    assert "npm ci --prefix /opt/agent-clis" in dockerfile


def test_linux_agent_cli_native_artifacts_are_required_dependencies() -> None:
    package = json.loads((_DOCKER_DIR / "agent-clis-package.json").read_text(encoding="utf-8"))
    lock = json.loads((_DOCKER_DIR / "agent-clis-package-lock.json").read_text(encoding="utf-8"))

    assert package["dependencies"]["@anthropic-ai/claude-code-linux-x64"] == "2.1.259"
    assert package["dependencies"]["@openai/codex-linux-x64"] == (
        "npm:@openai/codex@0.153.1-linux-x64"
    )
    assert "optional" not in lock["packages"]["node_modules/@anthropic-ai/claude-code-linux-x64"]
    assert "optional" not in lock["packages"]["node_modules/@openai/codex-linux-x64"]


def test_openroad_uses_verified_26q3_oci_artifact() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG OPENROAD_SOURCE_REF=a9147cf3aebe65e058bb3fa89c1f9e524488dbb8" in dockerfile
    assert "ARG OPENROAD_BINARY_VERSION=26Q2-2580-ga9147cf3ae" in dockerfile
    assert (
        "ARG OPENROAD_SOURCE_SENTINEL_SHA256="
        "c8bb060f372392663871afb62ca922f9da1fd58a1b635324da1ec713a88c928f"
    ) in dockerfile
    assert "./src/rsz/src/Resizer.tcl | sha256sum" in dockerfile
    assert (
        "COPY --from=openroad-artifacts /OpenROAD/build/bin/openroad /usr/bin/openroad"
        in dockerfile
    )
    assert "--exclude='./build'" in dockerfile
    assert "OpenROAD-a9147cf3aebe65e058bb3fa89c1f9e524488dbb8.tar.gz" in dockerfile
    assert "openroad -version" in dockerfile
    assert "COPY --from=openroad-artifacts /OpenROAD/src/sta/LICENSE" in dockerfile
    assert "Precision-Innovations/OpenROAD/releases/download" not in dockerfile
    assert "/tmp/openroad.deb" not in dockerfile


def test_cocotb_layer_overrides_openroad_parent_system_numpy() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    layer_start = dockerfile.index("# Cocotb verification layer")
    layer_end = dockerfile.index("# Build-time sanity", layer_start)

    assert "--ignore-installed" in dockerfile[layer_start:layer_end]


def test_agent_runtime_uses_validated_node24_and_executable_policy_probe() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    probe = Path("tests/docker/agent_policy_probe.py").read_text(encoding="utf-8")

    assert "ARG NODE_VERSION=24.20.0" in dockerfile
    assert (
        "ARG NODE_SHA256=2f2c0da162318f0de47665410c7c8c2ed3d36c8f3105de4bbc61176c70a7cbf2"
        in dockerfile
    )
    assert "--expected-node 24.20.0" in workflow
    assert "--expected-npm 11.19.0" in workflow
    assert "--network none" in workflow
    assert "agent_policy_probe.py" in workflow
    assert "--evidence /validation-tmp/agent-policy.json" in workflow
    assert "agent-policy-evidence-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert 'default="24.20.0"' in probe
    assert 'default="11.19.0"' in probe
    assert '"signal_exit_codes": _assert_signal_propagation(root)' in probe
    assert '["claude", "mcp", "serve"]' in probe
    assert '["codex", "mcp-server"]' in probe
    assert "os.kill(process.pid, signal.SIGTERM)" in probe
    assert "left descendants running after SIGTERM" in probe


def test_source_builds_fetch_immutable_commits() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    refs = dict(re.findall(r"^ARG ([A-Z0-9_]+_REF)=([0-9a-f]{40})$", dockerfile, re.MULTILINE))

    assert set(refs) == {
        "OPENROAD_SOURCE_REF",
        "YOSYS_REF",
        "ICARUS_REF",
        "VERILATOR_REF",
    }
    assert "git clone --depth 1 --branch" not in dockerfile
    for name in refs.keys() - {"OPENROAD_SOURCE_REF"}:
        assert f'git -c protocol.version=0 fetch --depth 1 origin "${{{name}}}"' in dockerfile
        assert f'test "$(git rev-parse HEAD)" = "${{{name}}}"' in dockerfile


def test_spike_uses_the_validated_snapshot_and_runs_upstream_checks() -> None:
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")
    spike_ref = re.search(r"^ARG SPIKE_REF=([0-9a-f]{40})$", riscv, re.MULTILINE)

    assert spike_ref is not None
    assert spike_ref.group(1) == "c09c0cce98696f52abe0fe8c11f93f9ed74dc2bb"
    assert 'git fetch --depth 1 origin "${SPIKE_REF}"' in riscv
    assert 'test "$(git rev-parse HEAD)" = "${SPIKE_REF}"' in riscv

    spike_build = riscv[riscv.index("ARG SPIKE_REF=") : riscv.index("# RISC-V International")]
    assert "make check" in spike_build
    assert "test -x /opt/riscv/bin/spike" in spike_build


def test_riscv_release_consumes_base_job_digest() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    assert "pip install build==1.6.0" in workflow
    assert "image-digest: ${{ steps.build.outputs.digest }}" in workflow
    assert "booley-sandbox=docker-image://" in workflow
    assert "@${{ needs.build-and-push.outputs.image-digest }}" in workflow
    assert "io.booley.build.parent-artifact-kind=registry-digest" in workflow
    assert (
        "io.booley.build.parent-artifact=${{ env.REGISTRY }}/${{ env.IMAGE_NAME }}@"
        "${{ needs.build-and-push.outputs.image-digest }}" in workflow
    )
    assert "steps.base-artifact.outputs.image-id" not in workflow


def test_release_base_records_exact_stable_runtime_parent() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    assert ("io.booley.build.parent-artifact=${{ steps.runtime-base.outputs.image }}") in workflow
    assert "runtime-base-artifact.outputs.image-id" not in workflow


def test_candidate_builds_consume_compatible_stable_base_by_immutable_digest() -> None:
    test_workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    release_workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    build_script = (_DOCKER_DIR / "build.sh").read_text(encoding="utf-8")
    contract_helper = Path("src/booley/harness/docker_base_contract.py").read_text(
        encoding="utf-8"
    )

    assert "docker_base_contract.py" in test_workflow
    assert "--resolve-image" in test_workflow
    assert "booley-runtime-base=docker-image://" in test_workflow
    assert "Dockerfile.base" in test_workflow
    assert "docker_base_contract.py" in release_workflow
    assert "--resolve-image" in release_workflow
    assert "booley-runtime-base=docker-image://" in release_workflow
    assert "@sha256:" in contract_helper
    assert "--build-context" in build_script
    assert "booley-runtime-base=docker-image://booley-runtime-base:local" in build_script
    assert "io.booley.runtime-base.image" in _DOCKERFILE.read_text(encoding="utf-8")


def test_changed_stable_base_build_reuses_trusted_cache_without_publishing() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    base_build = workflow[
        workflow.index("- name: Build changed stable runtime base locally") : workflow.index(
            "- name: Build candidate from changed stable base"
        )
    ]

    assert "uses: docker/build-push-action@" in base_build
    assert "load: true" in base_build
    assert "cache-from: type=gha,scope=sandbox-runtime-base" in base_build
    assert "cache-to:" not in base_build


def test_shared_candidate_cache_has_only_the_main_push_writer() -> None:
    test_workflow = yaml.safe_load(Path(".github/workflows/test.yml").read_text(encoding="utf-8"))
    candidate_build = next(
        step
        for step in test_workflow["jobs"]["bwave-smoke"]["steps"]
        if step.get("name") == "Build candidate from published stable base"
    )

    assert candidate_build["with"]["cache-from"] == "type=gha,scope=sandbox"
    assert candidate_build["with"]["cache-to"] == (
        "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' && "
        "'type=gha,scope=sandbox,mode=max,ignore-error=true' || '' }}"
    )

    release_workflow = yaml.safe_load(
        Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    )
    release_build = next(
        step
        for step in release_workflow["jobs"]["build-and-push"]["steps"]
        if step.get("id") == "build"
    )

    assert release_build["with"]["cache-from"] == "type=gha,scope=sandbox"
    assert "cache-to" not in release_build["with"]


def test_stacked_pr_builds_inherited_stable_base_locally() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    selection = workflow[
        workflow.index("- name: Select compatible stable runtime base") : workflow.index(
            "- name: Build changed stable runtime base locally"
        )
    ]

    assert "github.event_name == 'pull_request'" in selection
    assert "github.base_ref != 'main'" in selection


def test_stable_base_has_dedicated_publish_lifecycle_and_compatibility_smoke() -> None:
    workflow = Path(".github/workflows/docker-base-publish.yml").read_text(encoding="utf-8")

    assert "branches: [main]" in workflow
    assert "paths:" in workflow
    assert "Dockerfile.base" in workflow
    assert "boldaxolotl/booley-sandbox-base" in workflow
    assert "docker_base_contract.py" in workflow
    assert "group: publish-stable-docker-runtime-base" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "@${{ steps.build.outputs.digest }}" in workflow
    assert 'find_spec("booley") is None' in workflow
    assert "command -v yosys openroad iverilog verilator verible-verilog-lint" in workflow
    assert workflow.index("Verify exact published base") < workflow.index("Promote verified base")


def test_release_demo_installs_cli_at_trusted_host_prefix() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    trusted_cli_setup = """      - name: Install release CLI
        run: |
          python -m pip install --user .
          echo "${HOME}/.local/bin" >> "${GITHUB_PATH}"
"""
    assert trusted_cli_setup in workflow
    assert (
        'sudo install -o root -g root -m 0755 "${HOME}/.local/bin/booley" "/usr/bin/booley"'
    ) in workflow


def test_release_smokes_public_picorv32_demo_with_ci_owned_ticket() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    contract_check = "      - name: Verify exact reviewed demo contract\n"
    initialize = "      - name: Initialize demo cleanly as documented\n"
    staged_ownership = "      - name: Prepare staged demo ownership\n"
    host_doctor = "      - name: Run host Doctor issued-image probe\n"
    surface_smoke = "      - name: Run demo Doctor and ticket-authoring surface smoke\n"
    assert "uses: ./.github/actions/prepare-picorv32-demo" in workflow
    assert contract_check in workflow
    assert workflow.index(contract_check) < workflow.index(initialize)
    assert workflow.index(initialize) < workflow.index(staged_ownership)
    assert workflow.index(staged_ownership) < workflow.index(host_doctor)
    assert workflow.index(initialize) < workflow.index(surface_smoke)
    assert '"${RUNNER_TEMP}/booley-ci-bin/code"' in workflow
    assert 'cp -a demo "${RUNNER_TEMP}/booley-picorv32-demo"' in workflow
    assert "working-directory: ${{ runner.temp }}/booley-picorv32-demo" in workflow
    assert "set -o pipefail" in workflow
    assert 'booley init --skip-credentials | tee "${init_log}"' in workflow
    ownership_fragments = (
        'doctor_gid="$(getent passwd 1000 | cut -d: -f4)"',
        'echo "DOCTOR_GID=${doctor_gid}" >> "${GITHUB_ENV}"',
        'sudo chown -R "1000:${doctor_gid}" "${HOME}/.config/booley"',
        'sudo chown -R "1000:${doctor_gid}" "${RUNNER_TEMP}/booley-picorv32-demo"',
        'sudo chmod -R u+rwX "${RUNNER_TEMP}/booley-picorv32-demo"',
    )
    assert all(fragment in workflow for fragment in ownership_fragments)
    host_doctor_section = workflow[
        workflow.index(host_doctor) : workflow.index(
            "      - name: Measure release image storage contract\n"
        )
    ]
    identity_fragments = (
        "runner_groups=\"$(id -G | tr ' ' ',')\"",
        "/usr/bin/setpriv",
        '--reuid=1000 --regid="${DOCTOR_GID}"',
        '--groups="${DOCTOR_GID},${runner_groups}"',
        '"/usr/bin/booley" doctor --deep --skip-agent-checks',
        "      - name: Restore runner ownership after host Doctor\n        if: always()",
    )
    assert all(fragment in host_doctor_section for fragment in identity_fragments)
    assert 'grep -Fq "[!!]" "${init_log}"' in workflow
    assert workflow.count('doctor --deep --skip-agent-checks | tee "${doctor_log}"') == 2
    assert 'grep -Fq "0 warning(s)" "${doctor_log}"' in workflow
    assert 'grep -Fq "0 failed." "${doctor_log}"' in workflow
    assert "from booley.runtime.project_dir import resolve_project_dir" in workflow
    assert "BOOLEY_AGENT_APP=codex python -m booley.runtime.incontainer_register" in workflow
    assert "booley-ticket-create" in workflow
    assert "python -m booley.ticket_board validate-ticket" in workflow
    assert 'python -m booley.ticket_board show "${ticket_slug}"' in workflow
    assert workflow.count("bash /booley-source/.github/scripts/verify_picorv32_demo.sh") == 1
    contract_section = workflow[workflow.index(contract_check) : workflow.index(initialize)]
    assert "bash /booley-source/.github/scripts/verify_picorv32_demo.sh" in contract_section
    assert 'test "${before}" = "$(sha256sum "${ticket}")"' in workflow
    assert '--mount type=bind,src="${{ runner.temp }}/booley-picorv32-demo",dst=/work' in workflow
    assert "add-rv32-zbb-pcpi-co-processor" not in workflow
    assert "python -m booley.ticket_board parse-ticket" not in workflow
    assert (
        """      - name: Restore demo checkout ownership
        if: always()
        run: |
          if test -e "${RUNNER_TEMP}/booley-picorv32-demo"; then
            sudo chown -R "$(id -u):$(id -g)" "${RUNNER_TEMP}/booley-picorv32-demo"
          fi
"""
        in workflow
    )


def test_release_promotes_stable_tags_only_after_demo_smoke() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    build_section = workflow[: workflow.index("  demo-smoke:")]
    promote_section = workflow[workflow.index("  promote:") :]
    assert (
        ":candidate-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
        in build_section
    )
    assert ":latest" not in build_section
    assert "needs: [build-and-push, build-and-push-riscv, demo-smoke]" in promote_section
    assert "docker buildx imagetools create" in promote_section
    assert ":latest" in promote_section


def test_release_reports_registry_and_sidecar_image_sizes_after_initialization() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    initialize = "      - name: Initialize demo cleanly as documented\n"
    measure = "      - name: Measure release image storage contract\n"
    assert workflow.index(initialize) < workflow.index(measure)
    assert ".github/scripts/image_size_report.py" in workflow
    assert '--registry-image "sandbox=${BASE_IMAGE}"' in workflow
    assert '--registry-image "riscv=${RISCV_IMAGE}"' in workflow
    assert '--local-image "proxy=booley-egress-proxy"' in workflow
    assert '--local-image "reaper=booley-reaper"' in workflow
    assert "--limits .github/contracts/image-size-limits.toml" in workflow
    assert '--evidence "${RUNNER_TEMP}/booley-image-evidence/standard-contract.json"' in workflow
    assert '--evidence "${RUNNER_TEMP}/booley-image-evidence/riscv-contract.json"' in workflow
    assert 'cat "${RUNNER_TEMP}/booley-image-evidence/image-sizes.md"' in workflow
    assert "name: booley-image-sizes-${{ steps.version.outputs.version }}" in workflow

    pr_workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    assert "--limits .github/contracts/image-size-limits.toml" in pr_workflow
    assert ".github/scripts/image_runtime_resources.py" in pr_workflow
    standard_measure = "      - name: Record standard runtime resource observations\n"
    evidence_upload = "      - name: Upload Docker build evidence\n"
    assert pr_workflow.index(standard_measure) < pr_workflow.index(evidence_upload)
    assert "--image sandbox=booley-test" in pr_workflow


def test_candidate_ci_runs_openroad_physical_promotion_probe() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    probe = Path(".github/scripts/verify_openroad_runtime.sh").read_text(encoding="utf-8")

    assert "Run OpenROAD physical runtime probe" in workflow
    assert "verify_openroad_runtime.sh" in workflow
    assert (
        '--mount type=bind,src="${{ steps.nangate.outputs.root }}",dst=/opt/pdk,readonly'
        in workflow
    )
    assert "global_placement" in probe
    assert "detailed_placement" in probe
    assert 'run_openroad "repair-off" 0' in probe
    assert 'run_openroad "repair-on" 1' in probe
    assert "repair_timing -setup" in probe
    assert "report_design_area" in probe
    assert "QT_QPA_PLATFORM=offscreen openroad -gui -exit -no_init -no_splash /dev/null" in probe


def test_candidate_ci_runs_pinned_ibex_demo_offline() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "Prepare exact reviewed Ibex candidate" in workflow
    assert "repository: lowRISC/ibex" in workflow
    assert "ref: 34b0705760ef3dfa00e99637432473d2be8f22f3" in workflow
    assert "Run pinned Ibex lint demo" in workflow
    assert "--network none" in workflow
    assert "lowrisc:ibex:ibex_core" in workflow
    assert '--verilator_options="--Wno-fatal"' in workflow


def test_picorv32_demo_contract_runs_on_pr_main_merge_queue_and_nightly() -> None:
    workflow = Path(".github/workflows/picorv32-demo.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow
    assert "merge_group:" in workflow
    assert "schedule:" in workflow
    assert "uses: ./.github/actions/prepare-picorv32-demo" in workflow
    assert "bash /booley-source/.github/scripts/verify_picorv32_demo.sh" in workflow
