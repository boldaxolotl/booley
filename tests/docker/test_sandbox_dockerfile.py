"""Static contracts for the shipped Booley sandbox image."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

import yaml
from tests.sidecar_image_helpers import (
    DIND_IMAGE,
    SIDECAR_ALPINE_BASE,
    SIDECAR_BOOKWORM_BASE,
    SIDECAR_PYTHON_VERSION,
)

from booley.runtime.dockerfile_syntax import logical_instructions

_DOCKERFILE = Path("src/booley/data/docker/Dockerfile")
_DOCKER_DIR = _DOCKERFILE.parent
_BASE_DOCKERFILE = _DOCKER_DIR / "Dockerfile.base"
_SUBSTRATE_DOCKERFILE = _DOCKER_DIR / "Dockerfile.substrate"
_WHEEL_DOCKERFILE = _DOCKER_DIR / "Dockerfile.wheel"
_LIBEXEC_BWAVE = "/usr/local/libexec/booley/bwave"


def _workflow(path: str) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def _named_step(job: dict, name: str) -> dict:
    return next(step for step in job["steps"] if step.get("name") == name)


def test_claude_sdk_cli_duplicate_is_removed_in_install_layer() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    install_start = dockerfile.index("RUN python /tmp/booley-build/export_project_dependencies.py")
    install_end = dockerfile.index("\n\n# EDA invocation", install_start)
    install_layer = dockerfile[install_start:install_end]

    assert "CLAUDE_SDK_BUNDLED_CLI=" in install_layer
    assert 'rm -f "$CLAUDE_SDK_BUNDLED_CLI"' in install_layer
    assert 'test ! -e "$CLAUDE_SDK_BUNDLED_CLI"' in install_layer
    assert "python -m pip check" in install_layer
    assert "ClaudeSDKBackend" not in install_layer


def test_all_python_installs_tolerate_slow_publisher_reads() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    install_start = dockerfile.index("RUN python /tmp/booley-build/export_project_dependencies.py")
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
        "FROM docker.io/openroad/ubuntu26.04@sha256:"
        "c3185708e2f1cdb4fed9fb4d84d2882d36a1f1f6af90959ceb07dcb7416fe49d"
    ) in base
    assert (
        "FROM docker.io/openroad/ubuntu26.04-dev@sha256:"
        "a971c4db78c9a4a8db18a9fe136c9731c613e4582a5093180bc7c07e24351e09 "
        "AS eda-artifacts"
    ) in base
    assert (
        "FROM docker.io/library/ubuntu:26.04@sha256:"
        "f144425ff09be612d6d9ad965196e9cdc23dae1f42110a8a11a3e9a8198759f7"
    ) in base
    assert "COPY --from=eda-artifacts /usr/local/share/yosys/ /usr/local/share/yosys/" in base
    assert "COPY --from=eda-artifacts /usr/local/lib/ivl/ /usr/local/lib/ivl/" in base
    assert (
        "COPY --from=eda-artifacts /usr/local/share/verilator/ /usr/local/share/verilator/" in base
    )
    assert "COPY --from=openroad-artifacts /opt/or-tools/lib/ /opt/or-tools/lib/" in base
    assert "COPY --from=openroad-artifacts /opt/or-tools/include/" not in base
    assert "ARG VERIBLE_VERSION=v0.0-4296-g0f262651" in base
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
    assert 'test "$(claude --version | awk \'{print $1}\')" = "2.1.285"' in candidate
    assert "python -m pip check" in candidate


def test_stable_base_asserts_cocotb_icarus_library_contract() -> None:
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert 'test -e "$(cocotb-config --lib-name-path vpi icarus)"' in base
    assert "cocotb-config --lib-name vpi icarus" not in base
    assert "cocotb-config --lib-name-path vpi icarus).vpl" not in base


def test_stable_base_keeps_verilator_compiler_launcher_available() -> None:
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    contract = Path(".github/contracts/session-runtime.toml").read_text(encoding="utf-8")
    runtime_install = base[
        base.index(">>> Installing minimal runtime dependencies") : base.index(
            "# Copy only installed EDA payloads"
        )
    ]

    assert "ccache" in runtime_install
    assert "zlib1g-dev" in runtime_install
    assert '"ccache"' in contract
    assert '"/usr/include/zlib.h"' in contract


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


def test_split_recipes_create_bwave_paths_in_one_overlay_layer() -> None:
    """Issue #828: an exported layer cannot hold a hard link to a lower layer."""
    substrate = logical_instructions(_SUBSTRATE_DOCKERFILE.read_text(encoding="utf-8"))
    wheel = logical_instructions(_WHEEL_DOCKERFILE.read_text(encoding="utf-8"))
    legacy = logical_instructions(_DOCKERFILE.read_text(encoding="utf-8"))

    # The substrate carries no native B-Wave: the link partner would be stranded.
    assert not [i for i in substrate if "bwave-builder" in i.value]
    assert not [i for i in substrate if "/usr/local/libexec/booley" in i.value]
    assert not [i for i in substrate if i.keyword == "ENV" and "BOOLEY_BWAVE_BIN" in i.value]

    # Exactly one overlay RUN installs the binary and links the package-data path.
    installs = [i for i in wheel if i.keyword == "RUN" and "install -m 0755" in i.value]
    assert len(installs) == 1
    run = installs[0]
    assert "--mount=type=bind,from=bwave-builder" in run.value
    assert f"install -m 0755 /tmp/bwave/bwave {_LIBEXEC_BWAVE}" in run.value
    assert f'ln {_LIBEXEC_BWAVE} "$BWAVE_BIN_DIR/bwave"' in run.value
    assert not [i for i in wheel if i.keyword == "COPY" and "--from=bwave-builder" in i.value]
    assert not [
        i
        for i in wheel
        if i is not run and i.keyword == "RUN" and f"ln {_LIBEXEC_BWAVE}" in i.value
    ]

    env = next(i for i in wheel if i.keyword == "ENV" and "BOOLEY_BWAVE_BIN" in i.value)
    assert env.value == f"BOOLEY_BWAVE_BIN={_LIBEXEC_BWAVE}"
    assert env.line > run.line

    # The overlay builder stage matches the legacy recipe so BuildKit shares its cache.
    def builder_stage(instructions: tuple) -> list[tuple[str, str]]:
        start = next(n for n, i in enumerate(instructions) if "AS bwave-builder" in i.value)
        return [(i.keyword, i.value) for i in instructions[start : start + 3]]

    assert builder_stage(wheel) == builder_stage(legacy)


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

    assert release.count("sbom: true") == 4
    assert base_release.count("sbom: true") == 1


def test_ci_builds_and_tests_candidate_riscv_image_before_release() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    contract = Path(".github/scripts/verify_riscv_image_contract.sh").read_text(encoding="utf-8")
    demo = Path(".github/scripts/run_picorv32_ci_demo.sh").read_text(encoding="utf-8")
    verifier = Path(".github/scripts/verify_picorv32_demo.sh").read_text(encoding="utf-8")

    assert '--image "${IMAGE}"' in contract
    assert '--base-image "${BASE_IMAGE}"' in contract
    assert "--flavor riscv" in contract
    assert '--runtime-image "riscv=${IMAGE}"' in contract
    assert "verify_picorv32_demo.sh" in demo
    assert "-e BOOLEY_RUN_PICORV32_FLOWS=1" in demo
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


def test_readme_uses_current_slim_image_storage_guidance() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "15 GB of Docker storage" not in readme
    assert "21 GB" not in readme
    assert "about **4 GB** free for the image" in readme
    assert "(**6 GB** with the RISC-V toolchain)" in readme
    assert "On the measured containerd store" not in readme
    assert "1.58/2.02 GB" not in readme
    assert "2.82/4.48 GB" not in readme


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
    assert f'"{SIDECAR_PYTHON_VERSION}"' in evidence_script
    assert "source-repodigests.tsv" in evidence_script
    assert f'readonly DOCKER_DIND="{DIND_IMAGE}"' in evidence_script
    assert 'capture_source docker-dind "${DOCKER_DIND}"' in evidence_script
    assert f'readonly BOOKWORM_CANDIDATE="{SIDECAR_BOOKWORM_BASE}"' in evidence_script
    assert f'readonly ALPINE_CANDIDATE="{SIDECAR_ALPINE_BASE}"' in evidence_script
    assert (
        'readonly DOCKER_CLI="docker:29.8.2-cli@sha256:'
        'b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c"' in evidence_script
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
            if image in {
                "${BOOLEY_BASE_IMAGE}",
                "booley-runtime-base",
                "booley-standard-substrate",
                "booley-substrate",
                "booley-project-parent",
                "booley-layout-parent",
            }:
                continue
            assert re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image), (
                f"{path}: external base image is not digest-pinned: {image}"
            )


def test_reaper_uses_pinned_runtime_stages_without_live_package_install() -> None:
    reaper = (_DOCKER_DIR / "Dockerfile.reaper").read_text(encoding="utf-8")

    assert (
        "FROM docker:29.8.2-cli@sha256:"
        "b1805116a6a86cc591b5d5f60a910a0715cdcc9d18d866ad68b1457ead25c35c"
    ) in reaper
    assert "apk add" not in reaper
    assert "COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker" in reaper


def test_sidecars_pin_expected_python_bases_without_changing_distributions() -> None:
    egress = (_DOCKER_DIR / "Dockerfile.egress-proxy").read_text(encoding="utf-8")
    flexnet = (_DOCKER_DIR / "Dockerfile.flexnet-relay").read_text(encoding="utf-8")
    reaper = (_DOCKER_DIR / "Dockerfile.reaper").read_text(encoding="utf-8")

    assert f"FROM {SIDECAR_BOOKWORM_BASE}" in egress
    alpine = f"FROM {SIDECAR_ALPINE_BASE}"
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
    assert '"@anthropic-ai/claude-code": "2.1.285"' in lock
    assert '"@openai/codex": "0.160.0"' in lock
    assert lock.count('"integrity": "sha512-') == 16
    assert "npm ci --prefix /opt/agent-clis" in dockerfile


def test_linux_agent_cli_native_artifacts_are_required_dependencies() -> None:
    package = json.loads((_DOCKER_DIR / "agent-clis-package.json").read_text(encoding="utf-8"))
    lock = json.loads((_DOCKER_DIR / "agent-clis-package-lock.json").read_text(encoding="utf-8"))

    assert package["dependencies"]["@anthropic-ai/claude-code-linux-x64"] == "2.1.285"
    assert package["dependencies"]["@openai/codex-linux-x64"] == (
        "npm:@openai/codex@0.160.0-linux-x64"
    )
    assert "optional" not in lock["packages"]["node_modules/@anthropic-ai/claude-code-linux-x64"]
    assert "optional" not in lock["packages"]["node_modules/@openai/codex-linux-x64"]


def test_openroad_uses_verified_26q4_oci_artifact() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG OPENROAD_VERSION=26Q4" in dockerfile
    manifest = json.loads((_DOCKER_DIR / "openroad-source-manifest.json").read_text())
    assert manifest["revision"] == "c4d317e4fa2398b9880920a1411c20258a2175a9"
    assert len(manifest["submodules"]) == 5
    sta = next(item for item in manifest["submodules"] if item["path"] == "src/sta")
    assert sta["revision"] == "e983e15b2cb346badf8f23725fd3914d25312c44"
    assert "ARG OPENROAD_SOURCE_REF=c4d317e4fa2398b9880920a1411c20258a2175a9" in dockerfile
    assert "ARG OPENROAD_BINARY_VERSION=26Q3-2927-gc4d317e4fa" in dockerfile
    assert (
        "ARG OPENROAD_SOURCE_SENTINEL_SHA256="
        "8f72ff3a40bf361df9eee1d4a6ecf163285ebabca3d49c0f8eb958810f65a590"
    ) in dockerfile
    assert "./src/rsz/src/Resizer.tcl | sha256sum" in dockerfile
    assert (
        "COPY --from=openroad-artifacts /OpenROAD/build/bin/openroad /usr/bin/openroad"
        in dockerfile
    )
    assert "--exclude='./build' --exclude='./install'" in dockerfile
    assert "! grep -Eq '^\\./(build|install)(/|$)'" in dockerfile
    assert "OpenROAD-c4d317e4fa2398b9880920a1411c20258a2175a9.tar.gz" in dockerfile
    assert "openroad -version" in dockerfile
    assert "COPY --from=openroad-artifacts /OpenROAD/src/sta/LICENSE" in dockerfile
    assert "Precision-Innovations/OpenROAD/releases/download" not in dockerfile
    assert "/tmp/openroad.deb" not in dockerfile


def test_cocotb_layer_overrides_openroad_parent_system_numpy() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    layer_start = dockerfile.index("# Cocotb verification layer")
    layer_end = dockerfile.index("# Build-time sanity", layer_start)

    assert "--ignore-installed" in dockerfile[layer_start:layer_end]


def test_agent_runtime_uses_verified_node_and_executable_policy_probe() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    probe = Path("tests/docker/agent_policy_probe.py").read_text(encoding="utf-8")

    assert (
        "ARG NODE_SHA256=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6"
        in dockerfile
    )
    assert "--expected-npm 11.19.0" in workflow
    assert "--network none" in workflow
    assert "agent_policy_probe.py" in workflow
    assert "--evidence /validation-tmp/agent-policy.json" in workflow
    assert "agent-policy-evidence-${{ github.run_id }}-${{ github.run_attempt }}" in workflow
    assert 'default="11.19.0"' in probe


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
    assert spike_ref.group(1) == "609dbe0b9994154833039209fa37151e7c05e9d4"
    # The user-facing tool list names the exact pin, so it must move with it.
    supported_tools = Path("docs/user/SUPPORTED-EDA-TOOLS.md").read_text(encoding="utf-8")
    assert f"`{spike_ref.group(1)}`" in supported_tools
    assert 'git fetch --depth 1 origin "${SPIKE_REF}"' in riscv
    assert 'test "$(git rev-parse HEAD)" = "${SPIKE_REF}"' in riscv

    spike_build = riscv[riscv.index("ARG SPIKE_REF=") : riscv.index("# RISC-V International")]
    assert "make check" in spike_build
    assert "test -x /opt/riscv/bin/spike" in spike_build


def _from_images(contents: str) -> list[str]:
    return [
        instruction.value.split()[0]
        for instruction in logical_instructions(contents)
        if instruction.keyword == "FROM"
    ]


def test_riscv_tooling_builds_on_the_runtime_base_ubuntu_digest() -> None:
    """ADR 0070: tooling binaries link against the Ubuntu the base runs on."""
    base_images = _from_images(_BASE_DOCKERFILE.read_text(encoding="utf-8"))
    riscv_images = _from_images((_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8"))

    assert base_images[-1].startswith("docker.io/library/ubuntu:26.04@sha256:")
    assert riscv_images == [base_images[-1], "booley-standard-substrate"]


def test_riscv_final_stage_copies_only_from_the_tooling_stage() -> None:
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")
    instructions = logical_instructions(riscv)
    final_from = max(
        index for index, instruction in enumerate(instructions) if instruction.keyword == "FROM"
    )
    imports = [
        instruction.value for instruction in instructions if instruction.keyword in {"ADD", "COPY"}
    ]

    assert instructions[final_from].value == "booley-standard-substrate"
    assert imports == [
        "--from=riscv-tooling /opt/riscv /opt/riscv",
        "--from=riscv-tooling /opt/riscv-docs /opt/riscv-docs",
    ]
    assert all(
        "--mount" not in instruction.value
        for instruction in instructions
        if instruction.keyword == "RUN"
    )


def test_no_workflow_overrides_a_riscv_tooling_build_argument() -> None:
    """A --build-arg would change the tooling without changing its key."""
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")
    tooling_arguments = set(re.findall(r"^ARG ([A-Z0-9_]+)=", riscv, re.MULTILINE))
    assert {"SPIKE_REF", "XPACK_GCC_VERSION", "PSABI_SHA256"} <= tooling_arguments

    for path in [*Path(".github/workflows").glob("*.yml"), _DOCKER_DIR / "build-riscv.sh"]:
        text = path.read_text(encoding="utf-8")
        for argument in tooling_arguments:
            assert not re.search(rf"\b{argument}=", text), f"{path} overrides {argument}"


_RISCV_TOOLING_PUBLISHER = ".github/workflows/riscv-tooling-publish.yml"


def test_riscv_tooling_publisher_is_main_only_and_keyed_on_its_inputs() -> None:
    workflow = _workflow(_RISCV_TOOLING_PUBLISHER)
    # PyYAML's YAML 1.1 resolver reads the Actions key ``on`` as boolean true.
    events = workflow[True]
    key_job = workflow["jobs"]["key"]
    job = workflow["jobs"]["publish"]

    assert events["push"]["branches"] == ["main"]
    # The key reads Dockerfile.riscv through its own derivation and parser;
    # any of them can mint a new key, so each must trigger publication.
    assert set(events["push"]["paths"]) == {
        "src/booley/data/docker/Dockerfile.riscv",
        ".github/scripts/riscv_tooling.py",
        "src/booley/runtime/dockerfile_syntax.py",
        _RISCV_TOOLING_PUBLISHER,
    }
    assert "workflow_dispatch" in events
    assert key_job["if"] == "github.ref == 'refs/heads/main'"
    assert key_job["permissions"] == {"contents": "read"}
    assert job["needs"] == "key"
    assert job["permissions"] == {"contents": "read", "packages": "write"}
    # A workflow-wide group keeps only one pending run, so a queued key could
    # be dropped behind another key's publication. Group per key instead.
    assert "concurrency" not in workflow
    assert job["concurrency"] == {
        "group": "publish-riscv-tooling-${{ needs.key.outputs.key }}",
        "cancel-in-progress": False,
    }


def test_riscv_tooling_publisher_stages_verifies_then_promotes_without_overwrite() -> None:
    job = _workflow(_RISCV_TOOLING_PUBLISHER)["jobs"]["publish"]
    names = [step.get("name", step.get("uses", "")) for step in job["steps"]]
    order = [
        "Check for an existing tooling image",
        "Build and push candidate tooling image",
        "Verify candidate labels and digest",
        "Verify candidate tooling",
        "Record builder package versions",
        "Promote verified candidate to the key tag",
    ]
    assert [name for name in names if name in order] == order

    existing = _named_step(job, order[0])
    assert "riscv_tooling.py published" in existing["run"]
    for name in order[1:]:
        assert _named_step(job, name)["if"] == "steps.existing.outputs.state == 'absent'"

    build = _named_step(job, order[1])["with"]
    assert build["target"] == "riscv-tooling"
    # Digest-only push: no staging tag accumulates or resolves by name.
    assert "tags" not in build
    assert "push" not in build
    assert "push-by-digest=true" in build["outputs"]
    assert "push=true" in build["outputs"]
    # Role, key, and recipe labels come from the script, not retyped here.
    assert build["labels"].startswith("${{ needs.key.outputs.labels }}")
    assert "io.booley.riscv-tooling.key=" not in build["labels"]
    assert "cache-from" not in build
    assert "cache-to" not in build
    assert "build-args" not in build

    verify = _named_step(job, order[3])["run"]
    assert "riscv_tooling.py checks" in verify
    assert 'bash < "${checks}"' in verify

    promote = _named_step(job, order[5])["run"]
    assert promote.index("riscv_tooling.py published") < promote.index("imagetools create")
    assert promote.index("imagetools create") < promote.index("riscv_tooling.py promoted")
    assert "not overwriting" in promote
    assert "index:io.booley.riscv-tooling.builder-packages=" in promote
    assert '--tag "${FINAL}" "${CANDIDATE}"' in promote
    assert '--candidate "${CANDIDATE}"' in promote


def test_only_the_tooling_publisher_writes_riscv_tooling_tags() -> None:
    for path in Path(".github/workflows").glob("*.yml"):
        if path.as_posix() == _RISCV_TOOLING_PUBLISHER:
            continue
        for job in _workflow(str(path))["jobs"].values():
            for step in job.get("steps", []):
                pushes = step.get("with", {}).get("push") is True
                tags = str(step.get("with", {}).get("tags", ""))
                assert not (pushes and "riscv-tooling" in tags), f"{path} pushes tooling tags"
                run = str(step.get("run", ""))
                assert not ("imagetools create" in run and "riscv-tooling" in run), path

    test_workflow = _workflow(".github/workflows/test.yml")
    assert test_workflow["permissions"]["packages"] == "read"
    assert all(
        job.get("permissions", {}).get("packages") != "write"
        for job in test_workflow["jobs"].values()
    )


def test_release_build_dependency_is_pinned() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    assert "pip install build==1.6.1" in workflow


def test_local_build_script_resolves_compatible_stable_base() -> None:
    build_script = (_DOCKER_DIR / "build.sh").read_text(encoding="utf-8")
    contract_helper = Path("src/booley/runtime/docker_base_contract.py").read_text(
        encoding="utf-8"
    )

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

    assert candidate_build["with"]["cache-from"] == "type=gha,scope=sandbox-substrate"
    assert candidate_build["with"]["cache-to"] == (
        "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' && "
        "'type=gha,scope=sandbox-substrate,mode=max,ignore-error=true' || '' }}"
    )

    release_workflow = yaml.safe_load(
        Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")
    )
    release_build = next(
        step
        for step in release_workflow["jobs"]["build-and-push"]["steps"]
        if step.get("id") == "build"
    )

    assert release_build["with"]["cache-from"] == "type=gha,scope=sandbox-wheel"
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
    assert 'find_spec("booley") is None' in workflow
    assert "command -v yosys openroad iverilog verilator verible-verilog-lint" in workflow


def test_release_host_doctor_uses_only_an_isolated_installation_root() -> None:
    job = _workflow(".github/workflows/docker-publish.yml")["jobs"]["host-doctor-runtime"]
    wheel = _named_step(job, "Build official-release wheel from the candidate")["run"]
    prepare = _named_step(job, "Prepare isolated uid-1000 host")["run"]
    validate = _named_step(job, "Run isolated host validation")

    # Only an official-release wheel adopts the verified published image; a
    # runtime-image or development wheel would try to build it locally.
    assert "profile=BuildProfile.OFFICIAL_RELEASE" in wheel
    assert "python -m build --wheel --outdir dist/" in wheel
    assert 'root="${RUNNER_TEMP}/release-host-doctor"' in prepare
    assert 'mkdir -p "${root}/home" "${root}/evidence" "${root}/project"' in prepare
    # Bootstrap refuses venvs, so the host runs the wheel from the base interpreter.
    assert (
        'PYTHONUSERBASE="${root}/home/.local" python -m pip install --user dist/booley_rtl-*.whl'
    ) in prepare
    assert "venv" not in prepare + validate["run"]
    assert "pip install ." not in prepare
    assert '-- "${pythonLocation}/bin/python"' in validate["run"]
    assert (
        '--booley "${RUNNER_TEMP}/release-host-doctor/home/.local/bin/booley"' in validate["run"]
    )
    assert 'sudo chown -R "1000:${doctor_gid}" "${root}"' in prepare
    assert "/usr/bin/booley" not in prepare + validate["run"]
    assert (
        '"${GITHUB_WORKSPACE}/.github/scripts/release_validation/host_doctor.py"'
        in validate["run"]
    )


def test_release_demo_contracts_use_reviewed_fixture_and_behavior_modules() -> None:
    jobs = _workflow(".github/workflows/docker-publish.yml")["jobs"]
    surface = jobs["demo-ticket-surface"]
    flows = jobs["picorv32-demo-flows"]
    simulation = jobs["simulation-selftest-overlay"]

    for job in (surface, flows):
        prepare = _named_step(job, "Prepare exact reviewed demo contract")
        assert prepare["uses"] == "./.github/actions/prepare-picorv32-demo"
        assert job["needs"] == ["build-and-push", "build-and-push-riscv"]
    simulation_prepare = _named_step(simulation, "Prepare exact reviewed demo contract")
    assert simulation_prepare["with"] == {"materialize": True}
    assert (
        "-m release_validation.demo_surface"
        in _named_step(surface, "Validate immutable ticket surface")["run"]
    )

    simulation_run = _named_step(simulation, "Run Simulation Doctor self-tests")["run"]
    assert "/^\\[flows\\.synth\\]$/,/^\\[flows\\.fpga\\]$/" in simulation_run
    assert "enabled = false" in simulation_run
    assert "verify_picorv32_demo.sh" in _named_step(flows, "Run exact reviewed demo flows")["run"]
    assert _named_step(surface, "Restore demo ownership")["if"] == "always()"
    assert _named_step(flows, "Restore demo ownership")["if"] == "always()"
    assert _named_step(simulation, "Restore simulation ownership")["if"] == "always()"
    simulation_run = _named_step(simulation, "Run Simulation Doctor self-tests")["run"]
    assert 'cp -a demo "${RUNNER_TEMP}/simulation-project"' in simulation_run
    assert 'image = "booley-sandbox"' in simulation_run
    assert '--mount type=bind,src="${RUNNER_TEMP}/simulation-project",dst=/work' in simulation_run


def test_release_promotes_stable_tags_only_after_independent_gates() -> None:
    jobs = _workflow(".github/workflows/docker-publish.yml")["jobs"]
    standard_build = next(
        step for step in jobs["build-and-push"]["steps"] if step.get("id") == "build"
    )
    riscv_build = next(
        step for step in jobs["build-and-push-riscv"]["steps"] if step.get("id") == "build"
    )
    promotion = _named_step(jobs["promote"], "Promote exact tested digests without rebuilding")

    assert (
        ":candidate-${{ github.sha }}-${{ github.run_id }}-${{ github.run_attempt }}"
        in standard_build["with"]["tags"]
    )
    assert ":latest" not in standard_build["with"]["tags"] + riscv_build["with"]["tags"]
    assert set(jobs["promote"]["needs"]) - {"build-and-push", "build-and-push-riscv"} == {
        "standard-image-contract",
        "openroad-runtime",
        "host-doctor-runtime",
        "simulation-selftest-overlay",
        "helper-image-metadata",
        "riscv-image-contract",
        "demo-ticket-surface",
        "picorv32-demo-flows",
        "ibex-lint-demo",
    }
    assert "docker buildx imagetools create" in promotion["run"]
    assert ":latest" in promotion["run"]


def test_release_image_measurements_are_variant_scoped() -> None:
    jobs = _workflow(".github/workflows/docker-publish.yml")["jobs"]
    standard = _named_step(
        jobs["standard-image-contract"], "Validate provenance, runtime, size, and resources"
    )["run"]
    riscv = _named_step(jobs["riscv-image-contract"], "Validate exact RISC-V candidate")["run"]
    helper = _named_step(jobs["helper-image-metadata"], "Build and inspect helper images")

    assert "--limit-image sandbox" in standard
    assert '--registry-image "sandbox=${IMAGE}"' in standard
    assert "--limit-image riscv" in riscv
    assert '--registry-image "riscv=${RISCV_IMAGE}"' in riscv
    assert helper["run"] == "bash .github/scripts/sidecar-build-evidence.sh"

    pr_job = _workflow(".github/workflows/test.yml")["jobs"]["bwave-smoke"]
    standard_size = _named_step(pr_job, "Enforce standard image size ceiling")["run"]
    assert "--limit-image sandbox" in standard_size
    assert "--runtime-image sandbox=booley-test" in standard_size


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
    assert 'run_openroad "repair-off"' in probe
    assert 'run_openroad "repair-on"' in probe
    assert '(("repair-off", False), ("repair-on", True))' in probe
    assert "Design area" in probe
    assert "QT_QPA_PLATFORM=offscreen openroad -gui -exit -no_init -no_splash /dev/null" in probe
    assert "_build_yosys_script" in probe
    assert "write_openroad_script" in probe
    assert "u_probe_buffer" in probe
    assert "Removed [1-9][0-9]* buffers" in probe
    assert r"Wire dut.\intentional_undriven is used but has no driver" in probe
    assert "read_liberty -lib -nooverwrite -setattr booley_check_library" in probe
    assert "log_abc_dut.txt" in probe
    assert "abc-control" in probe
    assert 'ABC: Warning: Detected 2 multi-output cells (for example, "FA_X1").' in probe
    assert "collision-preserve" in probe
    assert "collision-attribute" in probe
    assert 'grep -Fq "Assertion failed"' in probe
    assert 'test ! -e "$work/collision-attribute/synth_collision_attribute.v"' in probe
    assert 'grep -Fq "assign Z = A;"' in probe
    assert "if grep -E '^\\[WARNING '" in probe
    assert '"$work"/check_dut_*.txt "$work"/yosys*.log' in probe
    assert '"$work"/log_abc_*.txt' in probe
    assert '"$work"/synth*.ys' in probe
    assert 'cp -R "$work/abc-control" "$work/collision-preserve"' in probe


def test_candidate_ci_runs_pinned_ibex_demo_offline() -> None:
    jobs = _workflow(".github/workflows/test.yml")["jobs"]
    smoke_job = jobs["bwave-smoke"]
    checkout = _named_step(smoke_job, "Prepare exact reviewed Ibex candidate")
    lint_command = _named_step(smoke_job, "Run pinned Ibex lint demo")["run"]
    command_argv = shlex.split(lint_command)
    inner_script = next(
        token for token in command_argv[command_argv.index("-c") + 1 :] if token.strip()
    )
    script_lines = inner_script.splitlines()

    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "path": "ibex-demo",
        "persist-credentials": False,
        "ref": "34b0705760ef3dfa00e99637432473d2be8f22f3",
        "repository": "lowRISC/ibex",
    }
    network_index = command_argv.index("--network")
    assert command_argv[network_index + 1] == "none"
    assert script_lines[0].rstrip().endswith("&& \\")
    assert script_lines[1].rstrip().endswith("--target=lint \\")
    assert "lowrisc:ibex:ibex_core" in script_lines[2]
    assert '--verilator_options="--Wno-fatal"' in script_lines[2]


def test_picorv32_demo_contract_runs_on_pr_main_merge_queue_and_nightly() -> None:
    workflow = Path(".github/workflows/picorv32-demo.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow
    assert "merge_group:" in workflow
    assert "schedule:" in workflow
    assert "uses: ./.github/actions/prepare-picorv32-demo" in workflow
    assert "bash /booley-source/.github/scripts/verify_picorv32_demo.sh" in workflow


def test_verilator_safe_release_and_fst_runtime_contract() -> None:
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    contract = Path(".github/contracts/session-runtime.toml").read_text(encoding="utf-8")
    assert "ARG VERILATOR_VERSION=v5.052" in base
    assert "ARG VERILATOR_REF=ea338be98e1e838d3518809ce8899f85a009963c" in base
    assert 'test "$(git rev-parse HEAD)" = "${VERILATOR_REF}"' in base
    assert base.count("liblz4-dev") == 2  # compiler stage and user model builds
    assert "libjemalloc-dev" not in base
    assert "/usr/include/lz4.h" in contract
    assert "/usr/local/share/verilator/BOOLEY-SOURCE.txt" in contract


def test_verilator_acceptance_is_required_in_candidate_image() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    assert "booley-test python /work/tests/docker/verilator_acceptance.py" in workflow
    assert "--work-dir /validation-tmp" in workflow
    assert "fifo native-fst verilator coverage-release simulator" in workflow


def test_layout_recipe_is_in_wheel_sources_and_package_data():
    import tomllib

    from booley.runtime.build_stamp import iter_wheel_source_files

    recipe = _DOCKER_DIR / "Dockerfile.project-data-layout"
    assert recipe in set(iter_wheel_source_files(Path()))
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert "data/**/*" in config["tool"]["setuptools"]["package-data"]["booley"]
    instructions = logical_instructions(recipe.read_text(encoding="utf-8"))
    assert instructions[0].keyword == "FROM"
    assert instructions[0].value == "booley-layout-parent"
    assert not any(line.keyword in {"COPY", "WORKDIR"} for line in instructions)
