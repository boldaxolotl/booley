"""Supply-chain and source-sync contracts for the shipped Booley sandbox image."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.devcontainer_spec_support import build_spec
from tests.subprocess_budget import run_bounded_child

from booley.runtime import dockerfile_syntax
from booley.runtime.dockerfile_syntax import logical_instructions

_DOCKER_DIR = Path("src/booley/data/docker")
_BASE_DOCKERFILE = _DOCKER_DIR / "Dockerfile.base"
_BASE_REQUIREMENTS = _DOCKER_DIR / "base-python-requirements.txt"
_BASE_REQUIREMENTS_IN_IMAGE = "/tmp/booley-build/base-python-requirements.txt"
_PIN_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PIN_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+")
_SANDBOX_DOCKERFILES = (
    "Dockerfile",
    "Dockerfile.base",
    "Dockerfile.riscv",
    "Dockerfile.substrate",
    "Dockerfile.wheel",
)


def _base_python_pins(contents: str) -> dict[str, str]:
    """Parse the base requirements file: blank, comment, or ``name==version`` lines.

    The Dockerfile ``sed`` extraction reads the same file with simple patterns,
    so only canonical lowercase names and exact numeric versions are accepted,
    one pin per package.
    """
    pins: dict[str, str] = {}
    for number, line in enumerate(contents.splitlines(), start=1):
        if not line.strip() or line.startswith("#"):
            continue
        name, separator, version = line.partition("==")
        if not separator or not _PIN_NAME.fullmatch(name) or not _PIN_VERSION.fullmatch(version):
            raise ValueError(f"requirements line {number}: expected 'name==version', got {line!r}")
        if name in pins:
            raise ValueError(f"requirements line {number}: duplicate pin {name!r}")
        pins[name] = version
    return pins


def test_base_python_installs_take_exact_pins_only_from_requirements_file() -> None:
    """No exact pin is written inline; every pinned package is installed via the file."""
    pins = _base_python_pins(_BASE_REQUIREMENTS.read_text(encoding="utf-8"))
    installed: set[str] = set()
    for instruction in logical_instructions(_BASE_DOCKERFILE.read_text(encoding="utf-8")):
        if instruction.keyword != "RUN" or not re.search(r"pip\s+install", instruction.value):
            continue
        if "--requirement /tmp/booley-build/project-dependencies.txt" in instruction.value:
            continue  # project dependency layer: pins come from pyproject.toml
        assert "==" not in instruction.value, instruction.value
        tokens = shlex.split(re.split(r"pip\s+install", instruction.value, maxsplit=1)[1])
        assert f"--constraint {_BASE_REQUIREMENTS_IN_IMAGE}" in instruction.value
        installed.update(token for token in tokens if re.fullmatch(r"[a-z][a-z0-9-]*", token))

    assert installed == set(pins)


def test_base_edalize_fusesoc_pins_match_project_metadata() -> None:
    pins = _base_python_pins(_BASE_REQUIREMENTS.read_text(encoding="utf-8"))
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    exact = dict(
        match.groups()
        for requirement in project["project"]["dependencies"]
        if (match := re.fullmatch(r"([A-Za-z0-9_.-]+)==([^;\s]+)", requirement))
    )

    for name in ("edalize", "fusesoc"):
        assert name in pins, f"{name} missing from base-python-requirements.txt"
        assert pins[name] == exact.get(name), name


def test_base_pyslang_pin_matches_optional_dependency_group() -> None:
    """ADR 0088: the Sandbox image and the extras install one pyslang, never the host."""
    pins = _base_python_pins(_BASE_REQUIREMENTS.read_text(encoding="utf-8"))
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"]

    assert "pyslang" in pins
    assert project["optional-dependencies"]["hdl-parser"] == [f"pyslang=={pins['pyslang']}"]
    assert not any(item.startswith("pyslang") for item in project["dependencies"])


def test_supported_tools_doc_lists_base_cocotb_pins() -> None:
    pins = _base_python_pins(_BASE_REQUIREMENTS.read_text(encoding="utf-8"))
    doc = Path("docs/user/SUPPORTED-EDA-TOOLS.md").read_text(encoding="utf-8")
    row = next(line for line in doc.splitlines() if line.startswith("| cocotb |"))

    assert f"| cocotb | {pins['cocotb']}, with" in row
    for name in ("cocotbext-axi", "cocotbext-uart", "numpy"):
        assert f"`{name}` {pins[name]}" in row, name


def test_stable_base_labels_match_the_external_from_references() -> None:
    """The in-file LABEL copies of the external FROM references stay in lockstep."""
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    stages = {
        reference.stage: reference.image for reference in dockerfile_syntax.from_references(base)
    }
    labels = dict(
        re.findall(r"^\s*(?:LABEL\s+)?(io\.booley\.[\w.-]+-artifact)=(\S+?)\s*\\?$", base, re.M)
    )
    assert labels == {
        "io.booley.openroad.builder-artifact": stages[0],
        "io.booley.eda.builder-artifact": stages[1],
        "io.booley.openroad.runtime-parent-artifact": stages[2],
    }


def _base_env(name: str) -> int:
    """Return the integer value of a ``name=value`` ENV in the runtime base image."""
    instructions = logical_instructions(_BASE_DOCKERFILE.read_text(encoding="utf-8"))
    values = [
        i.value.removeprefix(f"{name}=")
        for i in instructions
        if i.keyword == "ENV" and i.value.startswith(f"{name}=")
    ]
    assert len(values) == 1, f"expected one ENV {name}, found {values}"
    return int(values[0])


def test_claude_code_mcp_limits_outlast_booley_wait_windows() -> None:
    """Issue #2627: Claude Code must not cut a Booley MCP call short.

    MCP_TOOL_TIMEOUT stops the 60s kill; CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS
    stops the 120s move to a background task. Both must outlast the longest
    server-side wait (inline wait and the booley_poll ``wait_seconds`` cap).
    """
    from booley.mcp import catalog, server
    from booley.runtime import incontainer_setup

    longest_wait_ms = 1000 * max(
        server._DEFAULT_JOB_INLINE_WAIT_SECONDS,
        catalog._DEFAULT_JOB_POLL_WAIT_SECONDS,
        server._POLL_WAIT_SECONDS_MAX,
    )
    # One 2h client cap everywhere: image ENVs and the registrar's per-server timeout.
    registrar_timeout_ms = incontainer_setup.desired_claude_entry()["timeout"]
    assert _base_env("MCP_TOOL_TIMEOUT") == registrar_timeout_ms
    assert _base_env("CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS") == registrar_timeout_ms
    assert registrar_timeout_ms > longest_wait_ms
    # Claude Code clamps both settings to a 32-bit signed timer ceiling.
    assert registrar_timeout_ms <= 2**31 - 1


_DOWNLOAD = re.compile(r"\b(?:curl|wget)\s+-")
_CHECKSUM = re.compile(r'echo "\$\{(\w+)\}  (\S+)" \| sha256sum -c -')


def _download_runs() -> Iterator[tuple[str, str, str]]:
    """Yield (Dockerfile name, its text, RUN body) for every RUN that downloads a file."""
    for name in _SANDBOX_DOCKERFILES:
        text = (_DOCKER_DIR / name).read_text(encoding="utf-8")
        for instruction in logical_instructions(text):
            if instruction.keyword == "RUN" and _DOWNLOAD.search(instruction.value):
                yield name, text, instruction.value


def test_sandbox_downloads_are_verified_before_use() -> None:
    """Each downloaded file meets its pinned checksum before any later command names it."""
    runs = list(_download_runs())
    assert runs
    for name, text, body in runs:
        assert "||" not in body, f"{name}: a failed check could be swallowed: {body}"
        segments = [segment.strip() for segment in body.split("&&")]
        for index, segment in enumerate(segments):
            if not _DOWNLOAD.search(segment):
                continue
            for flag in ("--retry 8", "--retry-delay 30", "--retry-all-errors"):
                assert flag in segment, f"{name}: download without {flag}: {segment[:60]}"
            paths = re.findall(r"\s(?:-o|--output)[\s=]+(\S+)", segment)
            assert paths, f"{name}: download without a named output file: {segment[:60]}"
            for path in paths:
                later = next((item for item in segments[index + 1 :] if path in item), "")
                check = _CHECKSUM.fullmatch(later)
                assert check is not None and check[2] == path, f"{name}: {path} used unchecked"
                digest = dockerfile_syntax.argument_default(text, check[1])
                assert re.fullmatch(r"[0-9a-f]{64}", digest), f"{name}: {check[1]}"
    for name in _SANDBOX_DOCKERFILES:
        text = (_DOCKER_DIR / name).read_text(encoding="utf-8")
        assert "| bash" not in text, name
        assert "@^" not in text, name


_APT = re.compile(r"\bapt-get\s+(?:update|install)\b")
_GIT_NETWORK = re.compile(r"\bgit\b(?:\s+-c\s+\S+)*\s+(?:fetch|submodule\s+update)\b")
_APT_RETRIES = "Acquire::Retries"


def _network_retry_gaps(text: str) -> list[str]:
    """Name each apt or git network call in a stage that lacks a retry setup before it."""
    gaps: list[str] = []
    apt_ready = helper_ready = False
    for instruction in logical_instructions(text):
        if instruction.keyword == "FROM":
            apt_ready = helper_ready = False
        if instruction.keyword != "RUN":
            continue
        for segment in (part.strip() for part in instruction.value.split("&&")):
            if _APT_RETRIES in segment and "printf" in segment:
                apt_ready = True
            if "/usr/local/bin/booley-retry" in segment:
                helper_ready = True
            if _APT.search(segment) and not apt_ready:
                gaps.append(f"line {instruction.line}: apt without {_APT_RETRIES}")
            if _GIT_NETWORK.search(segment) and not segment.startswith("booley-retry "):
                gaps.append(f"line {instruction.line}: git without booley-retry")
            if segment.startswith("booley-retry ") and not helper_ready:
                gaps.append(f"line {instruction.line}: booley-retry used before it is written")
    return gaps


def test_image_network_commands_retry_transient_failures() -> None:
    """Every apt and git network call in an image recipe retries a transient failure."""
    for path in sorted(_DOCKER_DIR.glob("Dockerfile*")):
        assert _network_retry_gaps(path.read_text(encoding="utf-8")) == [], path.name
    bare = "FROM a\nRUN apt-get update && git fetch origin x\n"
    assert len(_network_retry_gaps(bare)) == 2


def test_agent_cli_installs_are_locked_with_integrity_hashes() -> None:
    lock = json.loads((_DOCKER_DIR / "agent-clis-package-lock.json").read_text(encoding="utf-8"))
    manifest = json.loads((_DOCKER_DIR / "agent-clis-package.json").read_text(encoding="utf-8"))

    assert lock["packages"][""]["dependencies"] == manifest["dependencies"]
    unverified = [
        path
        for path, package in lock["packages"].items()
        if path
        and package.get("version")
        and not package.get("integrity", "").startswith("sha512-")
    ]
    assert unverified == []
    assert "npm ci --prefix /opt/agent-clis" in _BASE_DOCKERFILE.read_text(encoding="utf-8")


def _base_build_arg(dockerfile: str, name: str) -> str:
    """Return the default of the one ``ARG name=value`` declaration in the base recipe."""
    return dockerfile_syntax.argument_default(dockerfile, name)


def test_bundled_vaporview_vsix_stays_in_lockstep_with_the_verified_build() -> None:
    """The image bundles exactly the build the spec installs and Booley patches."""
    from booley.runtime import vaporview

    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    version = _base_build_arg(dockerfile, "VAPORVIEW_VERSION")

    assert version == vaporview.VERIFIED_VERSION
    assert vaporview.BUNDLED_VSIX in _bundled_vsix_install_targets(dockerfile)


def _bundled_vsix_install_targets(dockerfile: str) -> set[str]:
    """Every VSIX path the base recipe installs, with ``ARG`` defaults substituted."""
    defaults = {
        argument.name: argument.default
        for argument in dockerfile_syntax.build_arguments(dockerfile)
        if argument.default is not None
    }
    targets = set()
    for target in re.findall(r'install -m 0644 /tmp/\S+\.vsix "([^"]+)"', dockerfile):
        targets.add(re.sub(r"\$\{(\w+)\}", lambda match: defaults[match.group(1)], target))
    return targets


def test_every_spec_extension_is_bundled_in_the_base_image() -> None:
    """No generated entry is a gallery ID: an egress-locked Sandbox cannot resolve one.

    Every entry must be a VSIX the image installs at exactly that path (so an
    image version that differs from its Python constant fails here) or the
    dashboard extension, which the wheel build supplies.
    """
    from booley.runtime import devcontainer
    from booley.runtime.dashboard_extension import VSIX_PATH

    bundled = _bundled_vsix_install_targets(_BASE_DOCKERFILE.read_text(encoding="utf-8"))
    for app in devcontainer.SUPPORTED_APPS:
        extensions = build_spec(app)["customizations"]["vscode"]["extensions"]
        assert [entry for entry in extensions if entry not in bundled | {VSIX_PATH}] == []


# name -> (ARG prefix, third-party notice member in the VSIX, notice file name shipped)
_MIT_VSIXES = {
    "veriloghdl": ("VERILOGHDL", "extension/THIRD_PARTY_NOTICES.md", "THIRD_PARTY_NOTICES.md"),
    "live-server": (
        "LIVE_PREVIEW",
        "extension/out/extension.js.LICENSE.txt",
        "extension.js.LICENSE.txt",
    ),
}


def _mit_vsix_fixture(path: Path, *, license_text: str, notice: bool) -> str:
    """Write a minimal VSIX zip; return its SHA-256."""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("extension/LICENSE.txt", license_text)
        if notice:
            archive.writestr("extension/THIRD_PARTY_NOTICES.md", "notices")
            archive.writestr("extension/out/extension.js.LICENSE.txt", "notices")
    return hashlib.sha256(path.read_bytes()).hexdigest()


_MIT_TEXT = "MIT License\n\nPermission is hereby granted, free of charge, to any person"


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("unzip") is None,
    reason="bundling requires a POSIX shell, sha256sum and unzip",
)
@pytest.mark.parametrize("name", _MIT_VSIXES)
@pytest.mark.parametrize("case", ["good", "checksum", "license", "missing-notice"])
def test_bundled_vsix_install_rejects_bad_payload(tmp_path: Path, name: str, case: str) -> None:
    """The shipped RUN body installs a good VSIX and refuses every bad payload."""
    prefix, _notice_member, notice_file = _MIT_VSIXES[name]
    instructions = logical_instructions(_BASE_DOCKERFILE.read_text(encoding="utf-8"))
    environment = os.environ.copy()
    for instruction in instructions:
        if instruction.keyword == "ARG" and "=" in instruction.value:
            key, value = instruction.value.split("=", 1)
            environment[key] = value
    fixture = tmp_path / "fixture.vsix"
    digest = _mit_vsix_fixture(
        fixture,
        license_text="GNU GENERAL PUBLIC LICENSE" if case == "license" else _MIT_TEXT,
        notice=case != "missing-notice",
    )
    environment[f"{prefix}_SHA256"] = "0" * 64 if case == "checksum" else digest
    environment["FIXTURE"] = str(fixture)
    body = next(
        (
            item.value
            for item in instructions
            if item.keyword == "RUN" and f"/tmp/{name}.vsix" in item.value
        ),
        None,
    )
    assert body is not None, f"Dockerfile.base never bundles {name}"
    root = tmp_path / "root"
    body = body.replace("/tmp/", f"{tmp_path}/").replace("/usr/local", str(root / "usr/local"))
    # Substitute the network download only; run the shipped verification commands.
    curl = 'curl() { for arg; do if [ "$arg" = "-o" ]; then shift; break; fi; shift; done; cp "$FIXTURE" "$1"; }; '
    result = run_bounded_child(
        ["sh", "-c", curl + body],
        env=environment,
    )
    installed = list((root / "usr/local/libexec/booley").glob("*.vsix"))
    assert (result.returncode == 0) == (case == "good"), result.stderr
    assert bool(installed) == (case == "good")
    if case == "good":
        licenses = root / "usr/local/share/booley/licenses" / name
        assert (licenses / "LICENSE.txt").read_text() == _MIT_TEXT
        assert (licenses / notice_file).read_text() == "notices"
        assert "MIT" in (licenses / "SOURCE.txt").read_text()


# Files that may name a CLI version besides the manifest: its lockfile, provenance
# comments, and historical records.
_CLI_VERSION_ALLOWED = (
    "src/booley/data/docker/agent-clis-package.json",
    "src/booley/data/docker/agent-clis-package-lock.json",
    "src/booley/config/gateway.py",
    "src/booley/runtime/text_only_agent.py",
    "docs/internals/GATEWAY.md",
    "CHANGELOG.md",
    "changelog.d/",  # entries become CHANGELOG.md text at release
    "docs/adr/",
    "docs/internals/validation/",
)


def test_agent_cli_versions_are_written_only_in_their_manifest() -> None:
    """No tracked file outside the allowlist names a pinned CLI version.

    Cargo.lock is excluded: Rust crate versions can coincide with a CLI version
    and cannot hold a CLI pin.
    """
    manifest = json.loads((_DOCKER_DIR / "agent-clis-package.json").read_text(encoding="utf-8"))
    for name in ("@anthropic-ai/claude-code", "@openai/codex"):
        version = manifest["dependencies"][name]
        # Digit boundaries stop a version from matching inside a longer one.
        pattern = rf"(^|[^0-9.]){re.escape(version)}([^0-9]|$)"
        result = run_bounded_child(
            ["git", "grep", "-l", "-E", pattern, "--", ".", ":!*Cargo.lock"],
        )
        assert result.returncode in (0, 1), result.stderr
        found = result.stdout.splitlines()
        # The search must see the manifest itself, or an empty match proves nothing.
        assert "src/booley/data/docker/agent-clis-package.json" in found
        stray = [path for path in found if not path.startswith(_CLI_VERSION_ALLOWED)]
        assert stray == [], f"{name} {version} is written outside the manifest: {stray}"


@pytest.mark.skipif(os.name == "nt", reason="the build check needs a POSIX shell")
def test_agent_cli_build_check_compares_installed_clis_with_the_manifest(tmp_path: Path) -> None:
    """Run the shipped build check against a fixture manifest and stub CLIs."""
    manifest_path = "/opt/agent-clis/package.json"
    checks = [
        instruction.value
        for instruction in logical_instructions(_BASE_DOCKERFILE.read_text(encoding="utf-8"))
        if instruction.keyword == "RUN" and manifest_path in instruction.value
    ]
    assert len(checks) == 1, "exactly one RUN must compare the installed CLIs with the manifest"

    manifest = tmp_path / "package.json"
    manifest.write_text(
        json.dumps(
            {"dependencies": {"@anthropic-ai/claude-code": "9.9.9", "@openai/codex": "8.8.8"}}
        ),
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

    def exit_code(claude_version: str, codex_version: str) -> int:
        for name, output in (
            ("claude", f"{claude_version} (Claude Code)"),
            ("codex", f"codex-cli {codex_version}"),
        ):
            stub = bin_dir / name
            stub.write_text(f"#!/bin/sh\necho '{output}'\n", encoding="utf-8")
            stub.chmod(0o755)
        return run_bounded_child(
            ["sh", "-c", checks[0].replace(manifest_path, str(manifest))],
            env=env,
        ).returncode

    assert exit_code("9.9.9", "8.8.8") == 0
    assert exit_code("9.9.8", "8.8.8") != 0, "claude mismatch must fail the build"
    assert exit_code("9.9.9", "8.8.7") != 0, "codex mismatch must fail the build"


def test_openroad_uses_verified_oci_artifact() -> None:
    dockerfile = _BASE_DOCKERFILE.read_text(encoding="utf-8")
    revision = dockerfile_syntax.argument_default(dockerfile, "OPENROAD_SOURCE_REF")

    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    manifest = json.loads((_DOCKER_DIR / "openroad-source-manifest.json").read_text())
    assert manifest["revision"] == revision
    assert re.fullmatch(
        r"[0-9a-f]{64}",
        dockerfile_syntax.argument_default(dockerfile, "OPENROAD_SOURCE_SENTINEL_SHA256"),
    )
    assert any(
        instruction.keyword == "RUN"
        and '"${OPENROAD_SOURCE_SENTINEL_SHA256}"' in instruction.value
        and "| sha256sum" in instruction.value
        for instruction in logical_instructions(dockerfile)
    )


_GIT_FETCH_OR_CLONE = re.compile(r"\bgit\s+(?:-c\s+\S+\s+)?(?:fetch|clone)\b")


def test_source_builds_fetch_immutable_commits() -> None:
    """Every source build pins a full commit and checks the fetched HEAD against it."""
    fetches = 0
    for name in ("Dockerfile.base", "Dockerfile.riscv"):
        dockerfile = (_DOCKER_DIR / name).read_text(encoding="utf-8")
        refs = re.findall(r"^ARG ([A-Z0-9_]+_REF)=(\S*)$", dockerfile, re.MULTILINE)
        assert refs, name
        for ref, value in refs:
            assert re.fullmatch(r"[0-9a-f]{40}", value), f"{name}: {ref}={value}"
        for instruction in logical_instructions(dockerfile):
            body = instruction.value
            if instruction.keyword != "RUN" or not _GIT_FETCH_OR_CLONE.search(body):
                continue
            assert "--branch" not in body, f"{name}: {body}"
            fetched = set(re.findall(r'\bfetch\b[^&]*"\$\{(\w+_REF)\}"', body))
            assert fetched, f"{name}: a source fetch does not name a pinned *_REF: {body}"
            for ref in fetched:
                assert f'test "$(git rev-parse HEAD)" = "${{{ref}}}"' in body, f"{name}: {ref}"
            fetches += 1
    assert fetches


def test_spike_pin_is_listed_in_supported_tools() -> None:
    """The user-facing tool list names the exact Spike pin, so it moves with it."""
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")
    spike_ref = dockerfile_syntax.argument_default(riscv, "SPIKE_REF")
    supported_tools = Path("docs/user/SUPPORTED-EDA-TOOLS.md").read_text(encoding="utf-8")

    assert f"`{spike_ref}`" in supported_tools


def test_riscv_tooling_builds_on_the_runtime_base_ubuntu_digest() -> None:
    """ADR 0070: tooling binaries link against the Ubuntu the base runs on."""
    base_images = [
        reference.image
        for reference in dockerfile_syntax.from_references(
            _BASE_DOCKERFILE.read_text(encoding="utf-8")
        )
    ]
    riscv_images = [
        reference.image
        for reference in dockerfile_syntax.from_references(
            (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")
        )
    ]

    assert re.search(r"@sha256:[0-9a-f]{64}$", base_images[-1])
    assert riscv_images[0] == base_images[-1]


def test_no_workflow_overrides_a_riscv_tooling_build_argument() -> None:
    """A --build-arg would change the tooling without changing its key."""
    riscv = (_DOCKER_DIR / "Dockerfile.riscv").read_text(encoding="utf-8")
    tooling_arguments = set(re.findall(r"^ARG ([A-Z0-9_]+)=", riscv, re.MULTILINE))
    assert tooling_arguments

    for path in [*Path(".github/workflows").glob("*.yml"), _DOCKER_DIR / "build-riscv.sh"]:
        text = path.read_text(encoding="utf-8")
        for argument in tooling_arguments:
            assert not re.search(rf"\b{argument}=", text), f"{path} overrides {argument}"


def test_verilator_identity_matches_the_dockerfile_pin() -> None:
    """The identity Booley checks at run time is the one the image builds."""
    from booley.flows.sim.verilator_identity import PINNED_VERILATOR

    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert (PINNED_VERILATOR.tag, PINNED_VERILATOR.commit) == (
        _base_build_arg(base, "VERILATOR_VERSION"),
        _base_build_arg(base, "VERILATOR_REF"),
    )


@pytest.mark.skipif(os.name == "nt", reason="document install requires POSIX shell and sha256sum")
@pytest.mark.parametrize(
    "bad_hash",
    [
        None,
        "ISA_UNPRIVILEGED_PDF_SHA256",
        "ISA_UNPRIVILEGED_HTML_SHA256",
        "ISA_PRIVILEGED_PDF_SHA256",
        "ISA_PRIVILEGED_HTML_SHA256",
    ],
)
def test_riscv_document_install_rejects_wrong_checksums(
    tmp_path: Path, bad_hash: str | None
) -> None:
    instructions = logical_instructions((_DOCKER_DIR / "Dockerfile.riscv").read_text())
    environment = os.environ.copy()
    for instruction in instructions:
        if instruction.keyword == "ARG" and "=" in instruction.value:
            name, value = instruction.value.split("=", 1)
            environment[name] = (
                hashlib.sha256(b"fixture").hexdigest() if name.endswith("_SHA256") else value
            )
    if bad_hash:
        environment[bad_hash] = "0" * 64
    install = next(
        item.value
        for item in instructions
        if item.keyword == "RUN" and "Downloading ratified" in item.value
    ).replace("/opt/riscv-docs", str(tmp_path / "docs"))
    # Substitute network downloads only; execute the shipped checksum and index commands.
    curl = 'curl() { for arg; do if [ "$arg" = "-o" ]; then shift; break; fi; shift; done; printf fixture > "$1"; }; '
    result = run_bounded_child(
        ["sh", "-c", curl + install],
        env=environment,
    )
    assert (result.returncode != 0) == (bad_hash is not None), result.stderr
    assert (tmp_path / "docs" / "INDEX.md").exists() == (bad_hash is None)
    if bad_hash is None:
        index = (tmp_path / "docs" / "INDEX.md").read_text()
        for volume in ("unprivileged", "privileged"):
            assert f"riscv-isa-{volume}.pdf / .html" in index
        assert environment["ISA_MANUAL_RELEASE"] in index


def test_agent_policy_probe_derives_node_and_npm_from_the_packaged_recipe() -> None:
    """The in-image probe expects exactly the versions the packaged recipe pins."""
    spec = importlib.util.spec_from_file_location(
        "agent_policy_probe", Path("tests/docker/agent_policy_probe.py")
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    base = _BASE_DOCKERFILE.read_text(encoding="utf-8")

    assert probe._recipe_versions() == (
        dockerfile_syntax.argument_default(base, "NODE_VERSION"),
        dockerfile_syntax.argument_default(base, "NPM_VERSION"),
    )
