"""Typed containerized EDA environment probes."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import StrEnum

from booley.audit.contracts import CommandRunner

RISCV_IMAGE_FIX = "rebuild the RISC-V image: ./src/booley/data/docker/build-riscv.sh"
RISCV_DOC_FILES = (
    "INDEX.md",
    "riscv-abi.pdf",
    "riscv-debug-specification.pdf",
    "riscv-isa-manual.html",
    "riscv-isa-manual.pdf",
)


class EdaFindingSeverity(StrEnum):
    """Presentation-independent severity for an EDA environment finding."""

    PASS = "pass"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class ContainerProbe:
    """One required tool or artifact probe inside an EDA image."""

    description: str
    command: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class EdaEnvironmentFinding:
    """Result of one containerized EDA environment probe."""

    severity: EdaFindingSeverity
    message: str
    fix: str = ""


def riscv_probe_specs() -> tuple[ContainerProbe, ...]:
    """Required tools and offline documents advertised by the RISC-V image."""
    documents = (
        "sh",
        "-c",
        'test -n "$BOOLEY_RISCV_DOCS" || exit 1; '
        'for name in "$@"; do test -s "$BOOLEY_RISCV_DOCS/$name" || exit 1; done',
        "sh",
        *RISCV_DOC_FILES,
    )
    return (
        ContainerProbe("riscv32-unknown-elf-gcc", ("riscv32-unknown-elf-gcc", "--version")),
        ContainerProbe("riscv64-unknown-elf-gcc", ("riscv64-unknown-elf-gcc", "--version")),
        ContainerProbe("srec_cat (srecord)", ("sh", "-c", "command -v srec_cat")),
        ContainerProbe("spike (riscv-isa-sim)", ("sh", "-c", "command -v spike")),
        ContainerProbe("pdftotext (poppler-utils)", ("pdftotext", "-v")),
        ContainerProbe("RISC-V offline specs complete at $BOOLEY_RISCV_DOCS", documents),
    )


def audit_riscv_toolchain(
    docker_exe: str,
    image: str,
    flavor: str | None,
    *,
    run: CommandRunner = subprocess.run,
) -> tuple[EdaEnvironmentFinding, ...]:
    """Probe RISC-V image promises when its baked flavor marker opts in."""
    if flavor != "riscv":
        return ()
    return tuple(_run_probe(docker_exe, image, probe, run) for probe in riscv_probe_specs())


def _run_probe(
    docker_exe: str,
    image: str,
    probe: ContainerProbe,
    run: CommandRunner,
) -> EdaEnvironmentFinding:
    try:
        result = run(
            [docker_exe, "run", "--rm", image, *probe.command],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return EdaEnvironmentFinding(
            EdaFindingSeverity.FAIL,
            f"{probe.description} (timeout/error)",
            RISCV_IMAGE_FIX,
        )
    if result.returncode == 0:
        return EdaEnvironmentFinding(EdaFindingSeverity.PASS, probe.description)
    return EdaEnvironmentFinding(
        EdaFindingSeverity.FAIL,
        probe.description,
        RISCV_IMAGE_FIX,
    )
