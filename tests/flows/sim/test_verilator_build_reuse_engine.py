"""End-to-end Verilator Simulation build reuse through ``SimulationExecution.run``.

Real Verilator is not required: each test installs a fake Verilator root
(wrapper, ``verilator_bin``, ``include/verilated.mk``) plus a fake C++ driver
that answers every identity probe, so the production
:func:`booley.flows.sim.build_reuse.verilator_identity` runs unpatched. The
fake ``verilator`` honours the stock Edalize recipe FuseSoC really writes: it
reads the ``.vc`` file, writes ``V<top>.mk`` and a ``V<top>__ver.d`` naming every
HDL file it read, and the ``.mk`` produces objects with ``-MMD``-style ``.d``
files plus an executable ``V<top>`` image. The image prints the marker word of
the source it was built from (``OLD``/``NEW``), so a stale launch is visible.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from booley.flows.base import SubprocessResult
from booley.flows.sim import build_reuse
from booley.flows.sim.build_session import simulation_build_slot
from booley.flows.sim.execution import (
    NamedTests,
    SimulationExecution,
    SimulationOptions,
    SimulationTargetOutcome,
)
from booley.targets.catalog import TargetCatalog
from booley.targets.domain import TargetHandle

pytestmark = pytest.mark.skipif(
    os.name == "nt", reason="Verilator build reuse is proven in the POSIX Sandbox"
)

_ADAPTER_MODULE = "booley.flows.sim.backends.verilator"

# The fake ``verilator``: answers ``--getenv VERILATOR_ROOT`` and otherwise
# Verilates the ``.vc`` recipe in its working directory (``--Mdir .``).
_FAKE_VERILATOR = r"""#!/usr/bin/env python3
import hashlib, pathlib, re, shlex, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
args = sys.argv[1:]
if args == ["--getenv", "VERILATOR_ROOT"]:
    print(ROOT)
    sys.exit(0)


def expand(tokens):
    out = []
    index = 0
    while index < len(tokens):
        if tokens[index] == "-f":
            text = pathlib.Path(tokens[index + 1]).read_text()
            out += expand(shlex.split(text, comments=True))
            index += 2
            continue
        out.append(tokens[index])
        index += 1
    return out


tokens = expand(args)
sources, cpp, incdirs, flags = [], [], [], []
top = None
index = 0
while index < len(tokens):
    token = tokens[index]
    if token == "--top-module":
        top = tokens[index + 1]
        index += 1
    elif token in ("-CFLAGS", "-LDFLAGS", "--Mdir", "--prefix", "-o", "--trace-depth"):
        flags.append(token + " " + tokens[index + 1])
        index += 1
    elif token.startswith("+incdir+"):
        incdirs += [item for item in token[len("+incdir+"):].split("+") if item]
    elif token.startswith(("-", "+")):
        flags.append(token)
    elif token.endswith((".cpp", ".cc", ".c")):
        cpp.append(token)
    else:
        sources.append(token)
    index += 1
prefix = "V" + top
read = []
text = ""
for source in sources:
    body = pathlib.Path(source).read_text()
    read.append(source)
    text += body
    for name in re.findall(r'`include\s+"([^"]+)"', body):
        found = next(
            str(pathlib.Path(d) / name) for d in incdirs if (pathlib.Path(d) / name).is_file()
        )
        read.append(found)
        text += pathlib.Path(found).read_text()
if "BAD" in text:
    sys.exit("%Error: BAD source")
marker = "NEW" if "NEW" in text else "OLD"
digest = hashlib.sha256(" ".join(flags).encode()).hexdigest()[:12]
pathlib.Path(prefix + ".cpp").write_text(f"// MARKER={marker} FLAGS={digest}\n{text}")
pathlib.Path(prefix + ".h").write_text("// header\n")
verilator_bin = ROOT / "bin" / "verilator_bin"
pathlib.Path(prefix + "__ver.d").write_text(
    f"{prefix}.cpp {prefix}.h : {verilator_bin} " + " ".join(read) + "\n"
)
skip_depfile = "NODEP" in text
cxx = ROOT / "bin" / "fake_compile"
objects = [prefix + ".o"]
rules = [f"{prefix}.o: {prefix}.cpp {prefix}.h\n\t{cxx} {prefix}.cpp {prefix}.o\n"]
for item in cpp:
    obj = pathlib.Path(item).stem + ".o"
    objects.append(obj)
    flag = " NODEP" if skip_depfile else ""
    rules.append(f"{obj}: {item}\n\t{cxx} {item} {obj}{flag}\n")
link = (
    f"{prefix}: {' '.join(objects)}\n"
    f"\t{cxx} --link {prefix} {' '.join(objects)}\n"
)
pathlib.Path(prefix + ".mk").write_text("".join([link, *rules]))
"""

# The object compiler/linker the fake ``.mk`` runs. Every object gets a
# Make-syntax ``.d`` unless the build asks for ``NODEP``.
_FAKE_COMPILE = r"""#!/usr/bin/env python3
import pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
args = sys.argv[1:]
if args[0] == "--link":
    image = pathlib.Path(args[1])
    text = "".join(pathlib.Path(obj).read_text() for obj in args[2:])
    marker = "NEW" if "MARKER=NEW" in text else "OLD"
    lines = [line for line in text.splitlines() if line.startswith("// ")]
    image.write_text(
        "#!/bin/sh\n"
        "echo '[SIM_RESULT] PASSED'\n"
        f"echo 'IMAGE={marker}'\n"
        + "".join(f"echo '{line[3:]}'\n" for line in lines)
    )
    image.chmod(0o755)
    sys.exit(0)
source, obj = pathlib.Path(args[0]), pathlib.Path(args[1])
obj.write_text(source.read_text())
prerequisites = [str(source)]
header = source.with_suffix(".h")
if header.is_file():
    prerequisites += [str(header), str(ROOT / "include" / "verilated.h")]
if "NODEP" not in args:
    obj.with_suffix(".d").write_text(f"{obj}: " + " \\\n ".join(prerequisites) + "\n")
"""

# A C++ driver that answers the identity probes ``verilator_identity`` runs.
_FAKE_CXX = """#!/bin/sh
case "$1" in
  --version) echo 'fakecxx 1.0'; exit 0 ;;
  -dumpmachine) echo 'x86_64-linux-gnu'; exit 0 ;;
  -print-search-dirs) echo 'install: @ROOT@/'; exit 0 ;;
  -print-prog-name=*) echo "$0"; exit 0 ;;
  -print-file-name=*) echo "${1#-print-file-name=}"; exit 0 ;;
  -E)
    echo '#include <...> search starts here:' >&2
    echo ' @SYSINC@' >&2
    echo 'End of search list.' >&2
    exit 0 ;;
esac
exit 1
"""


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _write_fake_verilator(root: Path) -> Path:
    """Install a fake Verilator root and C++ driver; return the PATH entry."""
    vroot = root / "vroot"
    (vroot / "bin").mkdir(parents=True)
    (vroot / "include").mkdir()
    sysinc = root / "sysinc"
    sysinc.mkdir()
    (sysinc / "cstdio").write_text("// system header\n", encoding="utf-8")
    _write_executable(vroot / "bin" / "verilator", _FAKE_VERILATOR)
    _write_executable(vroot / "bin" / "verilator_bin", "#!/bin/sh\nexit 0\n")
    _write_executable(vroot / "bin" / "fake_compile", _FAKE_COMPILE)
    (vroot / "include" / "verilated.h").write_text("// verilated\n", encoding="utf-8")
    (vroot / "include" / "verilated.mk").write_text(
        "CXX = fakecxx\nLINK = fakecxx\nAR = ar\n", encoding="utf-8"
    )
    tools = root / "toolbin"
    tools.mkdir()
    _write_executable(
        tools / "fakecxx",
        _FAKE_CXX.replace("@ROOT@", str(vroot)).replace("@SYSINC@", str(sysinc)),
    )
    return vroot / "bin"


def _write_project(root: Path, *, options: str = "--timing") -> Path:
    """A Verilator Target with an include, an include dir, and a DPI source."""
    project = root / "project"
    (project / ".booley_project").mkdir(parents=True)
    (project / ".booley_project" / "booley.toml").write_text("", encoding="utf-8")
    for name, text in (
        ("rtl/tb.sv", '`include "defs.svh"\nmodule tb; // OLD\nendmodule\n'),
        ("inc/defs.svh", "// DEFS v1\n"),
        ("dpi/helper.cpp", "// HELPER v1\n"),
    ):
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        (project / name).write_text(text, encoding="utf-8")
    _write_core(project, options=options)
    return project


def _write_core(project: Path, *, options: str) -> None:
    """Declare the Target through the Edalize flow API, as Booley setup does."""
    (project / "demo.core").write_text(
        "CAPI=2:\n"
        "name: acme:lib:demo:1\n"
        "filesets:\n"
        "  tb:\n"
        "    files:\n"
        "      - rtl/tb.sv: {file_type: systemVerilogSource}\n"
        "      - inc/defs.svh: {file_type: systemVerilogSource, is_include_file: true}\n"
        "      - dpi/helper.cpp: {file_type: cppSource}\n"
        "targets:\n"
        "  sim:\n"
        "    flow: sim\n"
        "    flow_options:\n"
        "      tool: verilator\n"
        f"      verilator_options: [{options}]\n"
        "    filesets: [tb]\n"
        "    toplevel: tb\n",
        encoding="utf-8",
    )


def _edit_keeping_mtime(path: Path, text: str) -> None:
    """Change bytes without moving mtime, so only content hashing can notice."""
    old = path.stat()
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns))


class _Recorder:
    """Run each command for real and record build stages and launched images."""

    def __init__(self, project: Path) -> None:
        source_root = Path(__file__).resolve().parents[3] / "src"
        self._cwd = project
        self._python_path = os.pathsep.join(
            part for part in (str(source_root), os.environ.get("PYTHONPATH", "")) if part
        )
        self.builds = 0
        self.launches: list[str] = []

    def __call__(self, command: list[str], *, timeout: int) -> SubprocessResult:
        started = time.monotonic()
        result = subprocess.run(
            command,
            cwd=self._cwd,
            env={**os.environ, "PYTHONPATH": self._python_path},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        self.builds += "BOOLEY_BUILD_STAGE" in command[-1]
        if _ADAPTER_MODULE in command[-1]:
            self.launches.append(result.stdout)
        return SubprocessResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_s=time.monotonic() - started,
        )


@pytest.fixture
def verilator_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put the fake toolchain on PATH and return the project root."""
    pytest.importorskip("fusesoc")
    pytest.importorskip("edalize")
    verilator_bin = _write_fake_verilator(tmp_path)
    path = os.pathsep.join((str(verilator_bin), str(tmp_path / "toolbin"), os.environ["PATH"]))
    monkeypatch.setenv("PATH", path)
    for name in ("VERILATOR_ROOT", "MAKEFLAGS", "MFLAGS", "EDALIZE_LAUNCHER"):
        monkeypatch.delenv(name, raising=False)
    # The fake installation is written moments before the first build; the
    # production race guard would (correctly) refuse to trust its files.
    monkeypatch.setattr(build_reuse, "RACE_MARGIN_NS", 0)
    return _write_project(tmp_path)


def _run(
    project: Path,
    recorder: _Recorder,
    *,
    test: str = "smoke",
    options: SimulationOptions | None = None,
) -> SimulationTargetOutcome:
    handle = _select(project)
    execution = SimulationExecution(
        invoke=recorder, options=options or SimulationOptions(timeout_ms=10_000)
    )
    return execution.run(handle, NamedTests((test,)))


def _select(project: Path) -> TargetHandle:
    return TargetCatalog.build(project).select("sim", for_flow="sim")


def _assert_passed(outcome: SimulationTargetOutcome) -> None:
    assert outcome.passed, (outcome.builds, outcome.tests, outcome.infrastructure_failure)


def _current_manifest(project: Path) -> dict[str, object]:
    slot = simulation_build_slot(_select(project))
    pointer = json.loads((slot / "current.json").read_text(encoding="utf-8"))
    build_root = slot / "g" / pointer["generation"] / pointer["build_root"]
    return json.loads((build_root / ".booley-build-manifest.json").read_text(encoding="utf-8"))


def _flags_line(stdout: str) -> str:
    return next(line for line in stdout.splitlines() if "FLAGS=" in line)


def test_unchanged_verilator_target_reuses_verified_image(verilator_env: Path) -> None:
    recorder = _Recorder(verilator_env)
    first = _run(verilator_env, recorder, test="first")
    second = _run(verilator_env, recorder, test="second")
    _assert_passed(first)
    _assert_passed(second)
    assert recorder.builds == 1
    assert first.builds[0].ran is True
    assert second.builds[0].ran is False
    assert second.builds[0].cache_decision.startswith("hit;")
    assert all("IMAGE=OLD" in stdout for stdout in recorder.launches)
    manifest = _current_manifest(verilator_env)
    assert manifest["reusable"] is True
    closure = manifest["read_closure"]
    assert isinstance(closure, dict)
    assert any(key.endswith("rtl/tb.sv") for key in closure)
    assert any(key.endswith("inc/defs.svh") for key in closure)
    assert any(key.endswith("dpi/helper.cpp") for key in closure)


def test_one_file_rtl_edit_rebuilds_and_never_launches_old_image(verilator_env: Path) -> None:
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    _edit_keeping_mtime(
        verilator_env / "rtl" / "tb.sv", '`include "defs.svh"\nmodule tb; // NEW\nendmodule\n'
    )
    _assert_passed(_run(verilator_env, recorder))
    assert recorder.builds == 2
    assert "IMAGE=OLD" in recorder.launches[0]
    assert "IMAGE=NEW" in recorder.launches[1] and "IMAGE=OLD" not in recorder.launches[1]


def test_failed_rebuild_after_edit_never_launches_prior_image(verilator_env: Path) -> None:
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    _edit_keeping_mtime(
        verilator_env / "rtl" / "tb.sv", '`include "defs.svh"\nmodule tb; // BAD\nendmodule\n'
    )
    outcome = _run(verilator_env, recorder)
    assert not outcome.passed
    assert recorder.builds == 2
    assert len(recorder.launches) == 1


def test_changed_verilator_flag_rebuilds(verilator_env: Path) -> None:
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    _write_core(verilator_env, options="--timing, -DEXTRA_FLAG")
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 2
    assert second.builds[0].ran is True
    assert _flags_line(recorder.launches[1]) != _flags_line(recorder.launches[0])


@pytest.mark.parametrize(
    ("name", "text"),
    [("dpi/helper.cpp", "// HELPER v2\n"), ("inc/defs.svh", "// DEFS v2\n")],
    ids=["dpi-source", "included-header"],
)
def test_changed_dpi_source_or_included_header_rebuilds(
    verilator_env: Path, name: str, text: str
) -> None:
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    _edit_keeping_mtime(verilator_env / name, text)
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 2
    assert second.builds[0].ran is True
    assert text.strip().removeprefix("// ") in recorder.launches[1]


def test_changed_toolchain_identity_rebuilds(verilator_env: Path, tmp_path: Path) -> None:
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    _write_executable(tmp_path / "vroot" / "bin" / "verilator_bin", "#!/bin/sh\nexit 1\n")
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 2
    assert second.builds[0].ran is True


@pytest.mark.parametrize("tamper", ["image", "read-closure"])
def test_corrupt_retained_generation_forces_fresh_build(verilator_env: Path, tamper: str) -> None:
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    slot = simulation_build_slot(_select(verilator_env))
    pointer = json.loads((slot / "current.json").read_text(encoding="utf-8"))
    build_root = slot / "g" / pointer["generation"] / pointer["build_root"]
    if tamper == "image":
        _write_executable(
            build_root / "Vtb", "#!/bin/sh\necho '[SIM_RESULT] PASSED'\necho STALE\n"
        )
    else:
        (build_root / "Vtb.h").write_text("// tampered generated header\n", encoding="utf-8")
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 2
    assert "STALE" not in recorder.launches[1]


def test_pre_sim_output_in_run_cwd_keeps_reuse(verilator_env: Path) -> None:
    (verilator_env / ".booley_project" / "booley.toml").write_text(
        '[flows.sim]\npre_run_commands = ["printf fw > \\"$BOOLEY_RUN_CWD/firmware.hex\\""]\n',
        encoding="utf-8",
    )
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 1
    assert second.builds[0].ran is False
    assert second.builds[0].cache_decision.startswith("hit;")


def test_pre_sim_output_in_build_root_forces_rebuild(verilator_env: Path) -> None:
    (verilator_env / ".booley_project" / "booley.toml").write_text(
        '[flows.sim]\npre_run_commands = ["printf fw > \\"$BOOLEY_BUILD_ROOT/firmware.hex\\""]\n',
        encoding="utf-8",
    )
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 2
    assert second.builds[0].ran is True


def test_object_without_dependency_record_is_never_reused(verilator_env: Path) -> None:
    _edit_keeping_mtime(
        verilator_env / "rtl" / "tb.sv",
        '`include "defs.svh"\nmodule tb; // OLD NODEP\nendmodule\n',
    )
    recorder = _Recorder(verilator_env)
    _assert_passed(_run(verilator_env, recorder))
    slot = simulation_build_slot(_select(verilator_env))
    assert not (slot / "current.json").exists()
    manifests = list((slot / "g").rglob(".booley-build-manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["reusable"] is False
    assert "helper.d" in str(manifest["reason"])
    second = _run(verilator_env, recorder)
    _assert_passed(second)
    assert recorder.builds == 2


def test_trace_and_plain_each_reuse_their_own_generation(verilator_env: Path) -> None:
    """The trace overlay is its own slot; neither variant evicts the other.

    No native B-Wave binary is needed to prove reuse: the traced runs launch
    their image (and then report the missing waveform), which is enough to see
    which generation each call selected.
    """
    recorder = _Recorder(verilator_env)
    outcomes = [
        _run(verilator_env, recorder, options=SimulationOptions(timeout_ms=10_000, trace=trace))
        for trace in (False, True, False, True)
    ]
    plain_first, trace_first, plain_again, trace_again = outcomes
    _assert_passed(plain_first)
    _assert_passed(plain_again)
    assert recorder.builds == 2
    assert plain_first.builds[0].ran and trace_first.builds[0].ran
    for outcome in (plain_again, trace_again):
        assert outcome.builds[0].ran is False
        assert outcome.builds[0].cache_decision.startswith("hit;")
    assert len(recorder.launches) == 4
    plain_launches, trace_launches = recorder.launches[0::2], recorder.launches[1::2]
    assert all("booleytrace" not in stdout for stdout in plain_launches)
    assert all("booleytrace" in stdout and "+trace" in stdout for stdout in trace_launches)
    assert _flags_line(trace_launches[0]) == _flags_line(trace_launches[1])
    assert _flags_line(trace_launches[0]) != _flags_line(plain_launches[0])
