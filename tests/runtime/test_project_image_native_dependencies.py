"""Owner tests for the Project image native build-dependency hint (#3213).

Each test builds a ``NativeDependencyRequest`` and asserts on the returned
``DiagnosticReport`` only: the owner takes no Doctor callbacks or ``ProjectAudit``.
"""

from __future__ import annotations

from pathlib import Path

from booley.audit.diagnostic_results import DiagnosticReport, Severity
from booley.runtime import project_image as pi


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode())


def _inspect(
    root: Path, sources: tuple[str, ...], dockerfile_text: str | None = None
) -> DiagnosticReport:
    dockerfile = root / ".booley" / "docker" / "Dockerfile"
    if dockerfile_text is not None:
        _write(root, ".booley/docker/Dockerfile", dockerfile_text)
    return pi.inspect_native_dependencies(pi.NativeDependencyRequest(root, sources, dockerfile))


def _rows(report: DiagnosticReport) -> list[tuple[Severity, str, str, str | None, str | None]]:
    return [(f.severity, f.message, f.fix, f.check_id, f.subject) for f in report.findings]


def _missing_message(package: str, users: str) -> str:
    return (
        f"C/C++ sources need the native package '{package}' ({users}), "
        "which the base sandbox image does not carry. Baking Python "
        "requirements cannot pull in native deps — add it to "
        ".booley/docker/Dockerfile and rebuild, or the simulation "
        "build will fail on a missing header/library"
    )


def test_known_header_warns_with_package_and_source(tmp_path: Path) -> None:
    _write(tmp_path, "tb/harness.cc", "#include <libelf.h>\n")

    report = _inspect(tmp_path, ("tb/harness.cc",))

    assert _rows(report) == [
        (
            Severity.WARN,
            _missing_message("libelf-dev", "tb/harness.cc"),
            "",
            "sandbox.native-dependency-missing",
            "libelf-dev",
        )
    ]


def test_declared_package_passes_before_remaining_warnings(tmp_path: Path) -> None:
    _write(tmp_path, "tb/a.cc", "#include <libelf.h>\n")
    _write(tmp_path, "tb/b.cc", '#include "gmp.h"\n')

    report = _inspect(
        tmp_path,
        ("tb/a.cc", "tb/b.cc"),
        "FROM booley-sandbox\n"
        "RUN apt-get update \\\n"
        "    && apt-get install -y --no-install-recommends libelf-dev\n",
    )

    assert _rows(report) == [
        (
            Severity.PASS,
            "native build dependencies declared in project Dockerfile: libelf-dev",
            "",
            None,
            None,
        ),
        (
            Severity.WARN,
            _missing_message("libgmp-dev", "tb/b.cc"),
            "",
            "sandbox.native-dependency-missing",
            "libgmp-dev",
        ),
    ]


def test_plain_apt_install_ending_at_semicolon_declares_package(tmp_path: Path) -> None:
    _write(tmp_path, "tb/a.cc", "#include <libelf.h>\n")

    report = _inspect(
        tmp_path, ("tb/a.cc",), "FROM booley-sandbox\nRUN apt install -y libelf-dev; true\n"
    )

    assert [(f.severity, f.message) for f in report.findings] == [
        (Severity.PASS, "native build dependencies declared in project Dockerfile: libelf-dev")
    ]


def test_dockerfile_comment_does_not_declare_package(tmp_path: Path) -> None:
    _write(tmp_path, "tb/a.cc", "#include <libelf.h>\n")

    report = _inspect(tmp_path, ("tb/a.cc",), "FROM booley-sandbox\n# install libelf-dev later\n")

    assert [(f.severity, f.subject) for f in report.findings] == [(Severity.WARN, "libelf-dev")]


def test_clean_sources_pass_with_scanned_count(tmp_path: Path) -> None:
    _write(tmp_path, "a.c", "#include <cstdint>\n")
    _write(tmp_path, "B.CC", "#include <zlib.h>\n")
    _write(tmp_path, "rtl.sv", "#include <libelf.h>\n")

    report = _inspect(tmp_path, ("a.c", "a.c", "B.CC", "rtl.sv"))

    assert _rows(report) == [
        (
            Severity.PASS,
            "no known-missing native build dependencies in 2 C/C++ source(s)",
            "",
            None,
            None,
        )
    ]


def test_nothing_scanned_reports_nothing(tmp_path: Path) -> None:
    _write(tmp_path, "rtl/dut.sv", "module dut; endmodule\n")

    report = _inspect(tmp_path, ("rtl/dut.sv", "tb/missing.cc"))

    assert report.findings == ()


def test_scan_stops_at_file_cap(tmp_path: Path) -> None:
    clean = tuple(f"src/f{index}.c" for index in range(401))
    for rel in clean:
        _write(tmp_path, rel, "#include <cstdint>\n")
    _write(tmp_path, "src/late.c", "#include <libelf.h>\n")

    report = _inspect(tmp_path, (*clean, "src/late.c"))

    assert _rows(report) == [
        (
            Severity.PASS,
            "no known-missing native build dependencies in 400 C/C++ source(s)",
            "",
            None,
            None,
        )
    ]


def test_many_users_are_summarized(tmp_path: Path) -> None:
    sources = tuple(f"tb/u{index}.cc" for index in (4, 2, 0, 3, 1))
    for rel in sources:
        _write(tmp_path, rel, "#include <png.h>\n")

    report = _inspect(tmp_path, sources)

    assert [f.message for f in report.findings] == [
        _missing_message("libpng-dev", "tb/u0.cc, tb/u1.cc, tb/u2.cc (+2 more)")
    ]
