"""The core repository hygiene owner returns complete reports; its rules need no Doctor.

Every assertion reads the returned ``DiagnosticReport``: severity, message, fix,
check ID and subject. The owner module is imported inside a helper so this file
collects, and each test fails on its own, before the owner exists. Git state is
built with real repositories; nothing here patches Doctor.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip
from tests.subprocess_budget import run_bounded_child

from booley.audit.diagnostic_results import DiagnosticFinding, DiagnosticReport, Severity

pytestmark = pytest.mark.usefixtures("isolated_git_attributes")

_CORE_FILE_ID = "project.core-file-untracked"
_MEMORY_IMAGE_ID = "project.memory-image-untracked"
_ARTIFACT_ID = "project.committed-build-artifact"

_GOOD_CORE = """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
targets:
  sim: {flow: sim, filesets: [rtl]}
"""


def _inspect(root: Path) -> DiagnosticReport:
    """Run the owner on ``root``, importing it where used."""
    owner = importlib.import_module("booley.runtime.core_repository_hygiene")
    return owner.inspect_core_repository_hygiene(owner.CoreRepositoryHygieneRequest(root))


def _warns(report: DiagnosticReport, check_id: str) -> list[DiagnosticFinding]:
    """The WARN findings that carry ``check_id``."""
    return [
        finding
        for finding in report.findings
        if finding.severity is Severity.WARN and finding.check_id == check_id
    ]


def _passes(report: DiagnosticReport) -> list[str]:
    """The PASS messages, in order."""
    return [f.message for f in report.findings if f.severity is Severity.PASS]


def _git(root: Path, *args: str) -> None:
    """Run one git command in ``root``, failing the test when it fails."""
    run_bounded_child(["git", *args], cwd=root, check=True)


def _write_core(root: Path, text: str, name: str = "design.core") -> None:
    (root / name).write_text(text, encoding="utf-8", newline="\n")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def test_untracked_referenced_file_warns(tmp_path):
    # A real git repo: firmware.hex referenced by the .core but not tracked.
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "rtl" / "dut.sv", "module dut; endmodule\n")
    _write(tmp_path / "firmware" / "firmware.hex", "00\n")
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - rtl/dut.sv: {file_type: systemVerilogSource}
      - firmware/firmware.hex: {file_type: user}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    _git(tmp_path, "add", "rtl/dut.sv", "design.core")
    warns = _warns(_inspect(tmp_path), _CORE_FILE_ID)
    assert len(warns) == 1
    assert "untracked" in warns[0].message and "firmware.hex" in warns[0].message
    assert warns[0].subject == "firmware/firmware.hex"


def test_all_referenced_files_tracked_passes(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "rtl" / "dut.sv", "module dut; endmodule\n")
    _write_core(tmp_path, _GOOD_CORE)
    _git(tmp_path, "add", "-A")
    report = _inspect(tmp_path)
    assert not _warns(report, _CORE_FILE_ID)
    assert "all 1 .core-referenced on-disk file(s) tracked by git" in _passes(report)


def test_submodule_files_count_as_tracked(tmp_path):
    """A .core referencing a vendored submodule's file is NOT the untracked trap.

    `git ls-files` stops at the gitlink, so every file inside the submodule read
    as untracked and the warning's own claim ("a fresh clone will lack them")
    was false: `git submodule update` restores exactly these.
    """
    dep = tmp_path / "dep"
    _write(dep / "vendor.sv", "module vendor; endmodule\n")
    _git(dep, "init", "-q")
    _git(dep, "add", "-A")
    _git(dep, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dep")

    proj = tmp_path / "proj"
    proj.mkdir()
    _git(proj, "init", "-q")
    _git(proj, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(dep), "lib/dep")
    _write_core(
        proj,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl: {files: [lib/dep/vendor.sv: {file_type: systemVerilogSource}]}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    _git(proj, "add", "design.core")

    report = _inspect(proj)
    assert not _warns(report, _CORE_FILE_ID)
    assert _passes(report)

    # The real trap still fires inside a submodule: a file the submodule's own
    # repo does not track genuinely vanishes on a fresh `submodule update`.
    _write(proj / "lib" / "dep" / "generated.hex", "00\n")
    _write_core(
        proj,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - lib/dep/vendor.sv: {file_type: systemVerilogSource}
      - lib/dep/generated.hex: {file_type: user}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    warns = _warns(_inspect(proj), _CORE_FILE_ID)
    assert len(warns) == 1
    assert "generated.hex" in warns[0].message and "untracked" in warns[0].message


def test_stealth_core_files_judged_against_state_repo(tmp_path):
    """ADR 0036: a stealth core's files are tracked by the Booley Directory's OWN
    repo — asking the host repo about them is guaranteed noise. Reach-through
    symlinks count as tracked for the content behind them (a clone ships the
    link, and the link's target is host-tracked upstream source)."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "rtl" / "dut.sv", "module dut; endmodule\n")
    _git(tmp_path, "add", "-A")

    state_cores = tmp_path / ".booley" / "cores"
    _write(state_cores / "fw" / "boot.hex", "00\n")
    symlink_or_skip(state_cores / "rtl", "../../rtl", target_is_directory=True)
    _write_core(
        state_cores,
        """\
CAPI=2:
name: ::demo-booley:0
filesets:
  rtl: {files: [rtl/dut.sv: {file_type: systemVerilogSource}]}
  fw:
    files:
      - fw/boot.hex: {file_type: user, copyto: boot.hex}
targets:
  sim: {flow: sim, filesets: [rtl, fw]}
""",
    )
    state_dir = tmp_path / ".booley"
    _git(state_dir, "init", "-q")
    _git(state_dir, "add", "-A")

    assert not _warns(_inspect(tmp_path), _CORE_FILE_ID)

    # The vendored-data trap still fires inside the state zone: a fw image the
    # nested repo does NOT track would vanish from a fresh clone.
    _git(state_dir, "rm", "--cached", "-q", "cores/fw/boot.hex")
    warns = _warns(_inspect(tmp_path), _CORE_FILE_ID)
    assert len(warns) == 1
    assert "boot.hex" in warns[0].message and "untracked" in warns[0].message


def test_core_files_tracked_judges_relocated_state_repo(tmp_path):
    """#2917: files under a relocated (two-component) Booley Directory are judged by
    that directory's own repo, not the host's."""
    _git(tmp_path, "init", "-q")
    (tmp_path / "booley.toml").write_text('[project]\ndir = "tools/booley"\n', encoding="utf-8")
    state_dir = tmp_path / "tools" / "booley"
    state_cores = state_dir / "cores"
    _write(state_cores / "fw" / "boot.hex", "00\n")
    _write_core(
        state_cores,
        """\
CAPI=2:
name: ::demo-booley:0
filesets:
  fw:
    files:
      - fw/boot.hex: {file_type: user, copyto: boot.hex}
targets:
  sim: {flow: sim, filesets: [fw]}
""",
    )
    _git(state_dir, "init", "-q")
    _git(state_dir, "add", "-A")

    assert not _warns(_inspect(tmp_path), _CORE_FILE_ID)

    _git(state_dir, "rm", "--cached", "-q", "cores/fw/boot.hex")
    warns = _warns(_inspect(tmp_path), _CORE_FILE_ID)
    assert len(warns) == 1
    assert "boot.hex" in warns[0].message and "untracked" in warns[0].message


def _readmem_repo(root: Path) -> None:
    """A repo whose TB boots via $readmemh from a memory image NOT in any .core."""
    _git(root, "init", "-q")
    _write(
        root / "tb" / "tb.sv",
        'module tb; reg [7:0] mem[0:255];\n  initial $readmemh("fw/boot.vmem", mem);\nendmodule\n',
    )
    _write(root / "fw" / "boot.vmem", "00\n")
    _write_core(
        root,
        """\
CAPI=2:
name: ::demo:0
filesets:
  tb:
    files:
      - tb/tb.sv: {file_type: systemVerilogSource}
targets:
  sim: {flow: sim, filesets: [tb]}
""",
    )


def test_readmemh_untracked_image_warns(tmp_path):
    # SETUP-12: the memory image is $readmemh'd but listed in no .core fileset,
    # and it is untracked (gitignored-style). The core-file rule can't see it;
    # the readmemh rule must.
    _readmem_repo(tmp_path)
    _git(tmp_path, "add", "tb/tb.sv", "design.core")
    warns = _warns(_inspect(tmp_path), _MEMORY_IMAGE_ID)
    assert len(warns) == 1
    message = warns[0].message
    assert "$readmemh" in message and "boot.vmem" in message and "untracked" in message
    assert warns[0].subject == "fw/boot.vmem"


def test_readmemh_tracked_image_passes(tmp_path):
    _readmem_repo(tmp_path)
    _git(tmp_path, "add", "-A")
    report = _inspect(tmp_path)
    assert not _warns(report, _MEMORY_IMAGE_ID)
    assert any("$readmemh-referenced image" in message for message in _passes(report))


def test_readmemh_constructed_path_ignored(tmp_path):
    # A path built from a plusarg / format specifier can't be resolved statically.
    _git(tmp_path, "init", "-q")
    _write(
        tmp_path / "tb" / "tb.sv",
        "module tb; string f;\n"
        '  initial begin $value$plusargs("mem=%s", f); $readmemh(f, mem); end\n'
        '  initial $readmemh({basedir, "/x.vmem"}, mem2);\n'
        "endmodule\n",
    )
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  tb: {files: [tb/tb.sv: {file_type: systemVerilogSource}]}
targets:
  sim: {flow: sim, filesets: [tb]}
""",
    )
    # Neither call is a resolvable literal: no readmem row of either severity.
    report = _inspect(tmp_path)
    assert not _warns(report, _MEMORY_IMAGE_ID)
    assert not [f for f in report.findings if "$readmem" in f.message]


def _artifact_repo(root: Path, *, with_source: bool, vendored: bool = False) -> None:
    """A git repo whose .core references a tracked firmware.hex; source optional.

    ``vendored`` tags the hex ``tags: [vendored]`` in the .core (the explicit
    "upstream blob, no rebuildable source" opt-out of the heuristic — the
    CAPI2-valid marker, not the fusesoc-rejected bare ``vendored: true`` key).
    """
    _git(root, "init", "-q")
    _write(root / "rtl" / "dut.sv", "module dut; endmodule\n")
    _write(root / "firmware" / "firmware.hex", "00\n")
    if with_source:
        # sibling C + assembly source → the hex is a build output, not a blob.
        _write(root / "firmware" / "hello.c", "int main(){}\n")
        _write(root / "firmware" / "start.S", ".global _start\n")
    hex_attrs = "{file_type: user, tags: [vendored]}" if vendored else "{file_type: user}"
    _write_core(
        root,
        f"""\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - rtl/dut.sv: {{file_type: systemVerilogSource}}
      - firmware/firmware.hex: {hex_attrs}
targets:
  sim: {{flow: sim, filesets: [rtl]}}
""",
    )
    _git(root, "add", "-A")


def test_committed_build_artifact_with_sibling_source_warns(tmp_path):
    _artifact_repo(tmp_path, with_source=True)
    warns = _warns(_inspect(tmp_path), _ARTIFACT_ID)
    assert len(warns) == 1
    assert "built from in-repo source" in warns[0].message
    assert "firmware.hex" in warns[0].message
    assert warns[0].subject == "firmware/firmware.hex"


def test_committed_opaque_blob_without_source_stays_quiet(tmp_path):
    # A vendored hex with no sibling source may be the only option — don't nag.
    _artifact_repo(tmp_path, with_source=False)
    assert not _warns(_inspect(tmp_path), _ARTIFACT_ID)


def test_vendored_annotated_artifact_stays_quiet_despite_sibling_source(tmp_path):
    # `tags: [vendored]` is the maintainer asserting the blob is upstream-shipped
    # with no rebuildable in-repo source — exempt even with sibling C/asm around.
    _artifact_repo(tmp_path, with_source=True, vendored=True)
    assert not _warns(_inspect(tmp_path), _ARTIFACT_ID)


def test_untracked_build_artifact_not_flagged_as_committed(tmp_path):
    # The artifact exists with sibling source but is NOT git-tracked → nothing to
    # call committed (the untracked trap is the core-file rule's job, not this).
    _artifact_repo(tmp_path, with_source=True)
    _git(tmp_path, "rm", "-q", "--cached", "firmware/firmware.hex")
    report = _inspect(tmp_path)
    assert not _warns(report, _ARTIFACT_ID)
    assert [w.subject for w in _warns(report, _CORE_FILE_ID)] == ["firmware/firmware.hex"]


def test_report_orders_the_three_rules_and_carries_check_ids(tmp_path):
    """One repo trips all three rules; the report lists them core-files, memory-image,
    build-artifact, each a fix-less WARN with its check ID and a sorted subject."""
    _git(tmp_path, "init", "-q")
    _write(tmp_path / "rtl" / "dut.sv", "module dut; endmodule\n")
    _write(
        tmp_path / "tb" / "tb.sv",
        'module tb; reg [7:0] mem[0:255];\n  initial $readmemh("fw/boot.vmem", mem);\nendmodule\n',
    )
    _write(tmp_path / "fw" / "boot.vmem", "00\n")
    _write(tmp_path / "firmware" / "firmware.hex", "00\n")
    _write(tmp_path / "firmware" / "hello.c", "int main(){}\n")
    _write_core(
        tmp_path,
        """\
CAPI=2:
name: ::demo:0
filesets:
  rtl:
    files:
      - rtl/dut.sv: {file_type: systemVerilogSource}
      - tb/tb.sv: {file_type: systemVerilogSource}
      - firmware/firmware.hex: {file_type: user}
targets:
  sim: {flow: sim, filesets: [rtl]}
""",
    )
    # Tracked: the artifact only. Untracked: dut.sv, tb.sv (core files), boot.vmem.
    _git(tmp_path, "add", "-f", "firmware/firmware.hex", "firmware/hello.c", "design.core")

    report = _inspect(tmp_path)
    assert [(f.severity, f.check_id, f.subject, f.fix) for f in report.findings] == [
        (Severity.WARN, _CORE_FILE_ID, "rtl/dut.sv,tb/tb.sv", ""),
        (Severity.WARN, _MEMORY_IMAGE_ID, "fw/boot.vmem", ""),
        (Severity.WARN, _ARTIFACT_ID, "firmware/firmware.hex", ""),
    ]


def test_outside_git_repository_emits_no_warning(tmp_path, monkeypatch):
    """Tracking is unknowable without a repository: no WARN, and the core-file rule
    keeps today's "all N ... tracked" PASS."""
    # A basetemp inside a work tree must not leak that repository in.
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    _write(tmp_path / "rtl" / "dut.sv", "module dut; endmodule\n")
    _write_core(tmp_path, _GOOD_CORE)
    report = _inspect(tmp_path)
    assert not [f for f in report.findings if f.severity is Severity.WARN]
    assert _passes(report) == ["all 1 .core-referenced on-disk file(s) tracked by git"]
