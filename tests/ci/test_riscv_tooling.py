"""Contracts for the RISC-V tooling-stage key (ADR 0070)."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

import riscv_tooling
from riscv_tooling import (
    DEFAULT_CONTRACT,
    DEFAULT_DOCKERFILE,
    ToolingRegistryError,
    ToolingStageError,
    main,
    published_image,
    stage_text,
    tag,
    tooling_checks,
    tooling_key,
    verify_promotion,
)

from booley.runtime.docker_base_contract import RemoteImage
from booley.runtime.image_provenance import resolve_recipe_fingerprint

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
    # newline="" keeps LF on Windows; the shipped Dockerfile is eol=lf.
    path.write_text(contents, encoding="utf-8", newline="")
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
    text = stage_text(DEFAULT_DOCKERFILE).decode("utf-8")

    assert text.startswith("FROM docker.io/library/ubuntu:26.04@sha256:")
    assert "ARG SPIKE_REF=" in text
    assert "ARG XPACK_GCC_VERSION=" in text
    assert "ARG PSABI_SHA256=" in text
    assert "booley-standard-substrate" not in text
    assert len(tooling_key(DEFAULT_DOCKERFILE)) == 64


def test_default_paths_do_not_depend_on_the_working_directory() -> None:
    script = _ROOT / ".github/scripts/riscv_tooling.py"

    result = subprocess.run(
        [sys.executable, str(script), "key"],
        cwd=_ROOT / "tests",
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"key={tooling_key(DEFAULT_DOCKERFILE)}\n" in result.stdout


def test_cli_writes_github_outputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    output = tmp_path / "github-output"
    output.write_text("existing=1\n", encoding="utf-8")

    assert main(["key", "--dockerfile", str(path), "--github-output", str(output)]) == 0

    key = tooling_key(path)
    fingerprint = resolve_recipe_fingerprint((path,))
    expected = (
        f"key={key}\ntag=riscv-tooling-{key}\n"
        f"image=ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{key}\n"
        "labels<<RISCV_TOOLING_LABELS\n"
        "io.booley.artifact.role=riscv-tooling\n"
        f"io.booley.riscv-tooling.key={key}\n"
        f"io.booley.build.recipe-fingerprint={fingerprint}\n"
        "RISCV_TOOLING_LABELS\n"
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
_VERIFIED = RemoteImage("ghcr.io/acme/base@sha256:" + "d" * 64, "sha256:" + "e" * 64)


def _resolver(outcome: object):
    """Fake the registry resolver: return ``outcome`` or raise it.

    ``outcome`` may also map references to per-reference outcomes.
    """
    calls: list[tuple[str, dict[str, str]]] = []

    def resolve(reference: str, labels: dict[str, str]) -> RemoteImage:
        calls.append((reference, dict(labels)))
        result = outcome.get(reference) if isinstance(outcome, dict) else outcome
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, RemoteImage)
        return result

    return resolve, calls


def _registry_failure(stderr: str) -> subprocess.CalledProcessError:
    return subprocess.CalledProcessError(1, ["docker", "buildx"], output="", stderr=stderr)


def _missing(reference: str) -> subprocess.CalledProcessError:
    return _registry_failure(f"ERROR: {reference}: not found\n")


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


def test_only_the_exact_missing_tag_report_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve, _calls = _resolver(_missing(_REFERENCE))
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    assert published_image(_REFERENCE, "k") is None


@pytest.mark.parametrize(
    "stderr",
    [
        "ERROR: 503 Service Unavailable",
        "ERROR: failed to authorize: token endpoint not found",
        "ERROR: failed to solve: docker-container driver not found",
        f"ERROR: {_REFERENCE}@sha256:{'f' * 64}: not found",
        "ERROR: MANIFEST_UNKNOWN: manifest unknown",
        f"WARNING: retrying\nERROR: {_REFERENCE}: not found",
    ],
    ids=["unavailable", "auth", "driver", "other-reference", "unqualified", "extra-lines"],
)
def test_other_registry_failures_are_not_absence(
    monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    """A misread failure would let the publisher overwrite a tag it could not see."""
    resolve, _calls = _resolver(_registry_failure(stderr))
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    with pytest.raises(ToolingRegistryError, match="cannot inspect"):
        published_image(_REFERENCE, "k")


def test_wrong_labels_are_an_integrity_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    resolve, _calls = _resolver(ValueError("io.booley.riscv-tooling.key label mismatch"))
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    with pytest.raises(ValueError, match="key label mismatch"):
        published_image(_REFERENCE, "k")


@pytest.mark.parametrize(
    ("outcome", "expected", "status"),
    [
        (
            _VERIFIED,
            f"state=present\ndigest_reference={_VERIFIED.digest_reference}\n"
            f"platform_manifest={_VERIFIED.platform_manifest}\n",
            0,
        ),
        ("missing", "state=absent\n", 0),
        (ValueError("io.booley.artifact.role label mismatch"), None, 1),
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
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    key = tooling_key(path)
    default = f"ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{key}"
    resolve, calls = _resolver(_missing(default) if outcome == "missing" else outcome)
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)
    output = tmp_path / "github-output"

    result = main(["published", "--dockerfile", str(path), "--github-output", str(output)])

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


_KEY = "0" * 64
_CANDIDATE = "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "c" * 64
_FINAL_TAG = f"ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{_KEY}"


def test_promotion_accepts_a_reindexed_tag_serving_the_verified_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The annotation changes the index digest but not the image manifest."""
    promoted = RemoteImage(
        "ghcr.io/boldaxolotl/booley-sandbox-base@sha256:" + "a" * 64,
        _VERIFIED.platform_manifest,
    )
    resolve, calls = _resolver({_FINAL_TAG: promoted, _CANDIDATE: _VERIFIED})
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    assert verify_promotion(_CANDIDATE, _KEY) == promoted
    assert [reference for reference, _labels in calls] == [_FINAL_TAG, _CANDIDATE]


@pytest.mark.parametrize(
    ("final", "error", "message"),
    [
        (
            RemoteImage(_VERIFIED.digest_reference, "sha256:" + "9" * 64),
            ValueError,
            "but the verified candidate is",
        ),
        (_missing(_FINAL_TAG), ToolingRegistryError, "missing after promotion"),
        (ValueError("label mismatch"), ValueError, "label mismatch"),
    ],
    ids=["different-image", "missing", "wrong-labels"],
)
def test_promotion_rejects_a_tag_not_serving_the_verified_image(
    monkeypatch: pytest.MonkeyPatch, final: object, error: type[Exception], message: str
) -> None:
    resolve, _calls = _resolver({_FINAL_TAG: final, _CANDIDATE: _VERIFIED})
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    with pytest.raises(error, match=message):
        verify_promotion(_CANDIDATE, _KEY)


def test_cli_promoted_reports_the_final_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    final = f"ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{tooling_key(path)}"
    resolve, _calls = _resolver({final: _VERIFIED, _CANDIDATE: _VERIFIED})
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)

    assert main(["promoted", "--dockerfile", str(path), "--candidate", _CANDIDATE]) == 0
    assert capsys.readouterr().out.startswith(f"reference={final}\nstate=present\n")


def _contract(tmp_path: Path, probes: str) -> Path:
    path = tmp_path / "contract.toml"
    path.write_text(
        '[riscv]\nrequired_paths = ["/opt/riscv-docs/INDEX.md", "/usr/bin/srec_cat"]\n' + probes,
        encoding="utf-8",
    )
    return path


def test_checks_render_tooling_paths_and_every_probe(tmp_path: Path) -> None:
    contract = _contract(
        tmp_path,
        "[[riscv.probe]]\nname = \"first\"\ncommand = '''\necho \"it's\"\n'''\n"
        "[[riscv.probe]]\nname = \"second\"\ncommand = 'true'\n",
    )

    script = tooling_checks(contract)

    assert "test -e /opt/riscv-docs/INDEX.md\n" in script
    assert "srec_cat" not in script
    assert script.count("bash -euo pipefail -c ") == 2
    assert "tooling check failed: first" in script


@pytest.mark.skipif(sys.platform == "win32", reason="renders a POSIX bash script")
@pytest.mark.parametrize(
    ("command", "status"),
    [("true", 0), ("false\ntrue", 1), ('set -- a b\ntest "$#" -eq 2', 0)],
    ids=["passes", "set-e-stops-a-probe", "positional-arguments"],
)
def test_rendered_checks_keep_errexit_inside_each_probe(
    tmp_path: Path, command: str, status: int
) -> None:
    """A probe that fails midway must fail the script, not fall through."""
    toml_command = command.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    contract = tmp_path / "contract.toml"
    contract.write_text(
        f'[riscv]\n[[riscv.probe]]\nname = "probe"\ncommand = "{toml_command}"\n',
        encoding="utf-8",
    )

    result = subprocess.run(
        ["bash"], input=tooling_checks(contract), capture_output=True, text=True, check=False
    )

    assert result.returncode == status, result.stderr


def test_shipped_contract_renders_tooling_checks() -> None:
    script = tooling_checks(DEFAULT_CONTRACT)

    assert "tooling check failed: RISC-V tooling shared libraries resolve" in script
    assert "find /opt/riscv -type f" in script
    assert "test -e /opt/riscv/lib/libriscv.so" in script


def _resolve_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outcome: object, *extra: str
) -> tuple[int, str, Path, list[tuple[str, dict[str, str]]]]:
    """Run ``resolve`` against a fake registry; return status, outputs, record, calls."""
    path = _dockerfile(tmp_path, _TOOLING + _FINAL)
    final = f"ghcr.io/boldaxolotl/booley-sandbox-base:riscv-tooling-{tooling_key(path)}"
    resolve, calls = _resolver(_missing(final) if outcome == "missing" else outcome)
    monkeypatch.setattr(riscv_tooling, "resolve_labeled_image_remote", resolve)
    output = tmp_path / "github-output"
    record = tmp_path / "evidence" / "tooling-source.json"
    status = main(
        [
            "resolve",
            "--dockerfile",
            str(path),
            "--record",
            str(record),
            "--github-output",
            str(output),
            *extra,
        ]
    )
    outputs = output.read_text(encoding="utf-8") if output.exists() else ""
    return status, outputs, record, calls


def test_resolve_uses_the_label_verified_digest_on_a_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, outputs, record, calls = _resolve_cli(tmp_path, monkeypatch, _VERIFIED)
    key = tooling_key(tmp_path / "Dockerfile.riscv")

    assert status == 0
    assert calls == [(riscv_tooling.final_image(key), riscv_tooling.expected_labels(key))]
    assert "source=registry\nreason=published\n" in outputs
    # Only the immutable digest reaches BuildKit, never the mutable tag.
    assert f"context=docker-image://{_VERIFIED.digest_reference}\n" in outputs
    assert json.loads(record.read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "key": key,
        "reference": riscv_tooling.final_image(key),
        "source": "registry",
        "reason": "published",
        "digest_reference": _VERIFIED.digest_reference,
        "platform_manifest": _VERIFIED.platform_manifest,
    }


@pytest.mark.parametrize(
    ("outcome", "reason"),
    [
        ("missing", "absent"),
        (_registry_failure("ERROR: 503 Service Unavailable"), "registry-error"),
    ],
    ids=["absent", "unreachable"],
)
def test_resolve_builds_locally_when_the_registry_cannot_supply_the_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    outcome: object,
    reason: str,
) -> None:
    """Availability over speed: a registry problem never fails the pull request."""
    status, outputs, record, _calls = _resolve_cli(tmp_path, monkeypatch, outcome)

    assert status == 0
    assert f"source=local\nreason={reason}\n" in outputs
    assert "context=" not in outputs
    stored = json.loads(record.read_text(encoding="utf-8"))
    assert (stored["source"], stored["reason"]) == ("local", reason)
    assert "digest_reference" not in stored
    if reason == "registry-error":
        assert "503 Service Unavailable" in stored["detail"]
        assert "::warning::" in capsys.readouterr().out


def test_resolve_fails_on_a_tag_with_the_wrong_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A retagged image is an integrity failure, not a cache miss."""
    status, outputs, record, _calls = _resolve_cli(
        tmp_path, monkeypatch, ValueError("io.booley.riscv-tooling.key label mismatch")
    )

    assert status == 1
    assert outputs == ""
    assert not record.exists()
    assert "label mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("reason", ["cold", "registry-timeout"])
def test_resolve_force_local_skips_the_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    status, outputs, record, calls = _resolve_cli(
        tmp_path, monkeypatch, _VERIFIED, "--force-local", reason
    )

    assert status == 0
    assert calls == []
    assert f"source=local\nreason={reason}\n" in outputs
    assert json.loads(record.read_text(encoding="utf-8"))["key"] == tooling_key(
        tmp_path / "Dockerfile.riscv"
    )


def test_resolve_rejects_unknown_force_local_reasons(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        main(["resolve", "--record", str(tmp_path / "r.json"), "--force-local", "warm"])


def test_fallback_records_the_rejected_registry_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _status, _outputs, record, _calls = _resolve_cli(tmp_path, monkeypatch, _VERIFIED)

    assert main(["fallback", "--record", str(record), "--detail", "ldd: not found"]) == 0

    stored = json.loads(record.read_text(encoding="utf-8"))
    assert stored["source"] == "local-compat-fallback"
    assert stored["reason"] == "compat-check-failed"
    assert stored["detail"] == "ldd: not found"
    assert stored["rejected_digest_reference"] == _VERIFIED.digest_reference
    assert "digest_reference" not in stored
    assert "platform_manifest" not in stored


@pytest.mark.parametrize(
    "contents",
    [None, "not json", '{"schema_version": 1, "source": "local", "reason": "absent"}'],
    ids=["missing", "malformed", "not-a-registry-hit"],
)
def test_fallback_requires_a_registry_hit_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], contents: str | None
) -> None:
    record = tmp_path / "tooling-source.json"
    if contents is not None:
        record.write_text(contents, encoding="utf-8")

    assert main(["fallback", "--record", str(record), "--detail", "x"]) == 1
    assert "riscv_tooling:" in capsys.readouterr().err
