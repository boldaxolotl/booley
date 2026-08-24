"""Static contracts for the shipped Booley sandbox image."""

from __future__ import annotations

import re
from pathlib import Path

_DOCKERFILE = Path("src/booley/data/docker/Dockerfile")
_DOCKER_DIR = _DOCKERFILE.parent


def test_claude_sdk_cli_duplicate_is_removed_in_install_layer() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    install_start = dockerfile.index("RUN python -m ensurepip --default-pip")
    install_end = dockerfile.index("\n\n# EDA invocation", install_start)
    install_layer = dockerfile[install_start:install_end]

    assert "CLAUDE_SDK_BUNDLED_CLI=" in install_layer
    assert 'rm -f "$CLAUDE_SDK_BUNDLED_CLI"' in install_layer
    assert 'test ! -e "$CLAUDE_SDK_BUNDLED_CLI"' in install_layer
    assert "backend._cli_path" in install_layer


def test_shipped_external_base_images_are_digest_pinned() -> None:
    for path in sorted(_DOCKER_DIR.glob("Dockerfile*")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.startswith("FROM "):
                continue
            image = line.split()[1]
            # The RISC-V flavor deliberately consumes a locally built named
            # context; the release workflow maps that name to the exact digest
            # emitted by the base-image job.
            if image == "${BOOLEY_BASE_IMAGE}":
                continue
            assert re.fullmatch(r"[^@\s]+@sha256:[0-9a-f]{64}", image), (
                f"{path}: external base image is not digest-pinned: {image}"
            )


def test_reaper_uses_pinned_runtime_stages_without_live_package_install() -> None:
    reaper = (_DOCKER_DIR / "Dockerfile.reaper").read_text(encoding="utf-8")

    assert "apk add" not in reaper
    assert "COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker" in reaper


def test_sandbox_downloads_are_verified_before_use() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")

    assert "| bash" not in dockerfile
    assert "@^" not in dockerfile
    for checksum_arg in (
        "OPENROAD_SHA256",
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
    assert '"@anthropic-ai/claude-code": "2.1.234"' in lock
    assert '"@openai/codex": "0.147.0"' in lock
    assert lock.count('"integrity": "sha512-') == 16
    assert "npm ci --prefix /opt/agent-clis" in dockerfile


def test_source_builds_fetch_immutable_commits() -> None:
    dockerfile = _DOCKERFILE.read_text(encoding="utf-8")
    refs = dict(re.findall(r"^ARG ([A-Z0-9_]+_REF)=([0-9a-f]{40})$", dockerfile, re.MULTILINE))

    assert set(refs) == {
        "YOSYS_REF",
        "CUDD_REF",
        "OPENSTA_REF",
        "ICARUS_REF",
        "VERILATOR_REF",
    }
    assert "git clone --depth 1 --branch" not in dockerfile
    for name in refs:
        assert f'git fetch --depth 1 origin "${{{name}}}"' in dockerfile
        assert f'test "$(git rev-parse HEAD)" = "${{{name}}}"' in dockerfile


def test_riscv_release_consumes_base_job_digest() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    assert "pip install build==1.5.0" in workflow
    assert "image-digest: ${{ steps.build.outputs.digest }}" in workflow
    assert "booley-sandbox=docker-image://" in workflow
    assert "@${{ needs.build-and-push.outputs.image-digest }}" in workflow


def test_release_demo_installs_cli_at_trusted_host_prefix() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    trusted_cli_setup = """      - name: Install release CLI
        run: |
          python -m pip install --user .
          echo "${HOME}/.local/bin" >> "${GITHUB_PATH}"
"""
    assert trusted_cli_setup in workflow


def test_release_smokes_public_picorv32_demo_and_ticket_mode() -> None:
    workflow = Path(".github/workflows/docker-publish.yml").read_text(encoding="utf-8")

    assert "repository: YosysHQ/picorv32" in workflow
    assert "repository: boldaxolotl/booley-prj-picorv32" in workflow
    assert '"${RUNNER_TEMP}/booley-ci-bin/code"' in workflow
    assert 'booley init | tee "${init_log}"' in workflow
    assert 'grep -Fq "[!!]" "${init_log}"' in workflow
    assert 'booley doctor --deep --skip-agent-checks | tee "${doctor_log}"' in workflow
    assert 'grep -Fq "0 warning(s)" "${doctor_log}"' in workflow
    assert 'grep -Fq "0 failed." "${doctor_log}"' in workflow
    assert "from booley.runtime.project_dir import resolve_project_dir" in workflow
    assert 'bash "${project_dir}/hooks/post-setup.sh"' in workflow
    assert "python -m booley.ticket_board parse-ticket" in workflow
    assert 'python -m booley.ticket_board show "${ticket_slug}"' in workflow
    assert 'booley run --ticket "${ticket_slug}" --dry-run' in workflow
    assert 'test "${before}" = "$(sha256sum "${ticket}")"' in workflow
