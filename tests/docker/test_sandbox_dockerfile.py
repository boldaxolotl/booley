"""Static contracts for the shipped Booley sandbox image."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

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
    assert "verible-verilog-lint --version" in base
    assert "COPY dist/booley_rtl-*.whl" not in base
    assert "COPY crates/bwave/" not in base
    assert "COPY src/booley/data/edalize/verible.py" not in base

    assert "FROM booley-runtime-base" in candidate
    assert "COPY dist/booley_rtl-*.whl" in candidate
    assert "COPY crates/bwave/" in candidate
    assert "COPY src/booley/data/edalize/verible.py" in candidate
    assert "--no-deps" in candidate
    assert '--wheel "$WHEEL"' in candidate
    assert "ClaudeSDKBackend" not in candidate
    assert 'test -x "$(command -v claude)"' in candidate
    assert 'test "$(claude --version | awk \'{print $1}\')" = "2.1.251"' in candidate
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
    wheel_copy = dockerfile.index("COPY dist/booley_rtl-*.whl")
    patch_region = dockerfile[patch_start:wheel_copy]

    assert "/tmp/booley-build/verible.py" in patch_region
    assert "import booley" not in patch_region


def test_ci_captures_docker_cache_and_layer_evidence() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")

    assert "docker history --no-trunc" in workflow
    assert "docker image inspect booley-test" in workflow
    assert "docker-build-evidence" in workflow


def test_ci_builds_and_runs_sidecar_control_candidate_matrix() -> None:
    workflow = Path(".github/workflows/test.yml").read_text(encoding="utf-8")
    evidence_script = Path(".github/scripts/sidecar-build-evidence.sh").read_text(encoding="utf-8")

    assert "bash .github/scripts/sidecar-build-evidence.sh" in workflow
    assert "docker build --pull --no-cache" in evidence_script
    for dockerfile in (
        "Dockerfile.egress-proxy",
        "Dockerfile.flexnet-relay",
        "Dockerfile.reaper",
    ):
        assert dockerfile in evidence_script
    for tag in ("py313", "py314"):
        assert evidence_script.count(f":{tag}") >= 3
    assert '"Python 3.13.15"' in evidence_script
    assert '"Python 3.14.7"' in evidence_script
    assert "source-repodigests.tsv" in evidence_script
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

    assert "apk add" not in reaper
    assert "COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker" in reaper


def test_sidecars_pin_python_3_14_7_without_changing_distributions() -> None:
    egress = (_DOCKER_DIR / "Dockerfile.egress-proxy").read_text(encoding="utf-8")
    flexnet = (_DOCKER_DIR / "Dockerfile.flexnet-relay").read_text(encoding="utf-8")
    reaper = (_DOCKER_DIR / "Dockerfile.reaper").read_text(encoding="utf-8")

    assert (
        "FROM python:3.14.7-slim-bookworm@sha256:"
        "416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63" in egress
    )
    alpine = (
        "FROM python:3.14.7-alpine3.24@sha256:"
        "05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc"
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
    assert '"@anthropic-ai/claude-code": "2.1.251"' in lock
    assert '"@openai/codex": "0.151.0"' in lock
    assert lock.count('"integrity": "sha512-') == 16
    assert "npm ci --prefix /opt/agent-clis" in dockerfile


def test_openroad_uses_verified_26q3_oci_artifact() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert "ARG OPENROAD_SOURCE_REF=a9147cf3aebe65e058bb3fa89c1f9e524488dbb8" in dockerfile
    assert "ARG OPENROAD_BINARY_VERSION=26Q2-2580-ga9147cf3ae" in dockerfile
    assert (
        "ARG OPENROAD_SOURCE_SENTINEL_SHA256="
        "c8bb060f372392663871afb62ca922f9da1fd58a1b635324da1ec713a88c928f"
    ) in dockerfile
    assert '/OpenROAD/src/rsz/src/Resizer.tcl" | sha256sum -c -' in dockerfile
    assert "/OpenROAD/build/bin/openroad" in dockerfile
    assert "openroad -version" in dockerfile
    assert "/OpenROAD/src/sta/LICENSE" in dockerfile
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


def test_release_smokes_public_picorv32_demo_with_ci_owned_ticket() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    contract_check = "      - name: Verify exact reviewed demo contract\n"
    initialize = "      - name: Initialize demo cleanly as documented\n"
    surface_smoke = "      - name: Run demo Doctor and ticket-authoring surface smoke\n"
    assert "uses: ./.github/actions/prepare-picorv32-demo" in workflow
    assert contract_check in workflow
    assert workflow.index(contract_check) < workflow.index(initialize)
    assert workflow.index(initialize) < workflow.index(surface_smoke)
    assert '"${RUNNER_TEMP}/booley-ci-bin/code"' in workflow
    assert 'booley init --skip-credentials | tee "${init_log}"' in workflow
    assert 'grep -Fq "[!!]" "${init_log}"' in workflow
    assert 'booley doctor --deep --skip-agent-checks | tee "${doctor_log}"' in workflow
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
    assert "add-rv32-zbb-pcpi-co-processor" not in workflow
    assert "python -m booley.ticket_board parse-ticket" not in workflow
    assert (
        """      - name: Restore demo checkout ownership
        if: always()
        run: |
          if test -e demo; then
            sudo chown -R "$(id -u):$(id -g)" demo
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


def test_picorv32_demo_contract_runs_on_pr_main_merge_queue_and_nightly() -> None:
    workflow = Path(".github/workflows/picorv32-demo.yml").read_text(encoding="utf-8")

    assert "pull_request:" in workflow
    assert "branches: [main]" in workflow
    assert "merge_group:" in workflow
    assert "schedule:" in workflow
    assert "uses: ./.github/actions/prepare-picorv32-demo" in workflow
    assert "bash /booley-source/.github/scripts/verify_picorv32_demo.sh" in workflow
