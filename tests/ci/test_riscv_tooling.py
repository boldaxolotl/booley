"""Contracts for the RISC-V tooling-stage key (ADR 0070)."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

import riscv_tooling
from riscv_tooling import (
    DEFAULT_DOCKERFILE,
    ToolingRegistryError,
    ToolingStageError,
    main,
    published_image,
    stage_text,
    tag,
    tooling_key,
)

_DIGEST = "sha256:" + "a" * 64
_TOOLING = f"""FROM docker.io/library/ubuntu:26.04@{_DIGEST} AS riscv-tooling
# Toolchain pin.
ARG TOOL_VERSION=1.0
RUN echo "${{TOOL_VERSION}}" \\
    # comment inside a continuation stays inside the instruction
    && mkdir -p /opt/riscv
"""
_FINAL = """
# ---------------------------------------------------------------------------
# Final stage.
FROM booley-standard-substrate
USER root
COPY --from=riscv-tooling /opt/riscv /opt/riscv
USER agent
"""


def _dockerfile(tmp_path: Path, contents: str) -> Path:
    path = tmp_path / "Dockerfile.riscv"
    path.write_text(contents, encoding="utf-8")
    return path


def test_stage_text_is_exact_stage_bytes_without_trailing_boundary_comments(
    tmp_path: Path,
) -> None:
    path = _dockerfile(tmp_path, "# Header.\n\n" + _TOOLING + _FINAL)

    assert stage_text(path) == _TOOLING.encode("utf-8")


def test_stage_may_be_the_last_stage(tmp_path: Path) -> None:
    path = _dockerfile(tmp_path, _TOOLING)

    assert stage_text(path) == _TOOLING.encode("utf-8")


@pytest.mark.parametrize(
    "edit",
    [
        ("ARG TOOL_VERSION=1.0", "ARG TOOL_VERSION=1.1"),
        ("# Toolchain pin.", "# Toolchain pin, reworded."),
        ("mkdir -p", "mkdir  -p"),
        ("AS riscv-tooling\n", "AS riscv-tooling\n\n"),
        ("a" * 64, "b" * 64),
    ],
    ids=["arg", "comment", "whitespace", "blank-line", "ubuntu-digest"],
)
def test_any_stage_edit_changes_the_key(tmp_path: Path, edit: tuple[str, str]) -> None:
    original = tooling_key(_dockerfile(tmp_path, _TOOLING + _FINAL))
    edited = _TOOLING.replace(*edit, 1)
    assert edited != _TOOLING

    assert tooling_key(_dockerfile(tmp_path, edited + _FINAL)) != original


@pytest.mark.parametrize(
    "contents",
    [
        "# Changed header.\n" + _TOOLING + _FINAL,
        _TOOLING + _FINAL.replace("# Final stage.", "# Final stage, reworded."),
        _TOOLING + _FINAL.replace("USER agent", "USER agent\nWORKDIR /work"),
    ],
    ids=["header-comment", "boundary-comment", "final-stage"],
)
def test_edits_outside_the_stage_keep_the_key(tmp_path: Path, contents: str) -> None:
    original = tooling_key(_dockerfile(tmp_path, _TOOLING + _FINAL))

    assert tooling_key(_dockerfile(tmp_path, contents)) == original


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (_FINAL, "found 0"),
        (_TOOLING + _TOOLING + _FINAL, "found 2"),
        ("ARG UBUNTU=26.04\n" + _TOOLING + _FINAL, "global ARG"),
        (_TOOLING.replace(f"@{_DIGEST}", ""), "@sha256 digest"),
        (_TOOLING + "COPY patches/ /tmp/\n" + _FINAL, "COPY is not allowed"),
        (_TOOLING + "ADD https://example.invalid/x /tmp/x\n", "ADD is not allowed"),
        (_TOOLING + "RUN --mount=type=bind,target=/src true\n", "RUN --mount"),
        (_TOOLING + "RUN echo \\\n", "unterminated continuation"),
        ("# Only comments.\n", "no FROM"),
    ],
    ids=[
        "missing",
        "duplicate",
        "global-arg",
        "undigested",
        "copy",
        "add",
        "mount",
        "unterminated",
        "empty",
    ],
)
def test_unkeyable_stage_fails_closed(tmp_path: Path, contents: str, message: str) -> None:
    with pytest.raises(ToolingStageError, match=message):
        tooling_key(_dockerfile(tmp_path, contents))


def test_key_binds_schema_and_platform(tmp_path: Path) -> None:
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    unversioned = hashlib.sha256(_TOOLING.encode("utf-8")).hexdigest()

    assert tooling_key(path) != unversioned
    assert (
        tooling_key(path)
        == hashlib.sha256(
            b"riscv-tooling-v1\0linux/amd64\0" + _TOOLING.encode("utf-8")
        ).hexdigest()
    )


def test_tag_rejects_malformed_keys() -> None:
    assert tag("0" * 64) == "riscv-tooling-" + "0" * 64
    with pytest.raises(ToolingStageError, match="malformed"):
        tag("ABC")


def test_shipped_dockerfile_yields_a_key() -> None:
    text = stage_text(_ROOT / DEFAULT_DOCKERFILE).decode("utf-8")

    assert text.startswith("FROM docker.io/library/ubuntu:26.04@sha256:")
    assert "ARG SPIKE_REF=" in text
    assert "ARG XPACK_GCC_VERSION=" in text
    assert "ARG PSABI_SHA256=" in text
    assert "booley-standard-substrate" not in text
    assert len(tooling_key(_ROOT / DEFAULT_DOCKERFILE)) == 64


def test_cli_writes_github_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    output = tmp_path / "github-output"
    output.write_text("existing=1\n", encoding="utf-8")

    assert main(["key", "--dockerfile", str(path), "--github-output", str(output)]) == 0

    key = tooling_key(path)
    expected = (
        f"key={key}\ntag=riscv-tooling-{key}\n"
        f"image=ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{key}\n"
    )
    assert output.read_text(encoding="utf-8") == "existing=1\n" + expected
    assert capsys.readouterr().out == expected


def test_cli_reports_unkeyable_stage(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _dockerfile(tmp_path, _FINAL)
    output = tmp_path / "github-output"

    assert main(["key", "--dockerfile", str(path), "--github-output", str(output)]) == 1

    assert "found 0" in capsys.readouterr().err
    assert not output.exists()


_REFERENCE = "ghcr.io/acme/base:riscv-tooling-" + "0" * 64
_VERIFIED = "ghcr.io/acme/base@sha256:" + "d" * 64


def _resolver(outcome: object):
    """Fake the registry resolver: return a digest or raise ``outcome``."""
    calls: list[tuple[str, dict[str, str]]] = []

    def resolve(reference: str, labels: dict[str, str]) -> str:
        calls.append((reference, dict(labels)))
        if isinstance(outcome, BaseException):
            raise outcome
        return str(outcome)

    return resolve, calls


def _registry_failure(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["docker", "buildx"], output="", stderr=stderr)


def test_published_image_requires_role_and_key_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve, calls = _resolver(_VERIFIED)
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    assert published_image(_REFERENCE, "k") == _VERIFIED
    assert calls == [
        (
            _REFERENCE,
            {"io.booley.artifact.role": "riscv-tooling", "io.booley.riscv-tooling.key": "k"},
        )
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: ghcr.io/acme/base:riscv-tooling-x: not found",
        "ERROR: MANIFEST_UNKNOWN: manifest unknown",
    ],
)
def test_missing_tag_is_absent(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    resolve, _calls = _resolver(_registry_failure(stderr))
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    assert published_image(_REFERENCE, "k") is None


def test_other_registry_failures_are_not_absence(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve, _calls = _resolver(_registry_failure("ERROR: 503 Service Unavailable"))
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    with pytest.raises(ToolingRegistryError, match="503 Service Unavailable"):
        published_image(_REFERENCE, "k")


def test_wrong_labels_are_an_integrity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve, _calls = _resolver(ValueError("io.booley.riscv-tooling.key mismatch"))
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    with pytest.raises(ValueError, match="key mismatch"):
        published_image(_REFERENCE, "k")


@pytest.mark.parametrize(
    ("outcome", "expected", "status"),
    [
        (_VERIFIED, f"state=present\ndigest_reference={_VERIFIED}\n", 0),
        (_registry_failure("not found"), "state=absent\n", 0),
        (ValueError("io.booley.artifact.role mismatch"), None, 1),
        (_registry_failure("connection reset"), None, 1),
    ],
    ids=["present", "absent", "mismatch", "unreachable"],
)
def test_cli_reports_publication_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: object,
    expected: str | None,
    status: int,
) -> None:
    resolve, calls = _resolver(outcome)
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    output = tmp_path / "github-output"

    result = main(["published", "--dockerfile", str(path), "--github-output", str(output)])

    key = tooling_key(path)
    default = f"ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{key}"
    assert result == status
    assert calls[0][0] == default
    if expected is None:
        assert not output.exists()
        assert "riscv_tooling:" in capsys.readouterr().err
    else:
        assert output.read_text(encoding="utf-8") == f"reference={default}\n" + expected


def test_cli_inspects_an_explicit_candidate_reference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolve, calls = _resolver(_VERIFIED)
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)

    assert main(["published", "--dockerfile", str(path), "--reference", _REFERENCE]) == 0
    assert calls[0] == (_REFERENCE, riscv_tooling.expected_labels(tooling_key(path)))
