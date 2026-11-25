"""Schema-aware discovery of Project-controlled program inputs."""

from pathlib import Path

import pytest
from tests.conftest import symlink_or_skip

from booley.targets.declared_inputs import core_program_paths, project_config_program_paths


def _core_paths(root: Path, document: dict, **options) -> tuple[Path, ...]:
    return core_program_paths(document, core_file=root / "toy.core", project_root=root, **options)


def _write(root: Path, *relative: str) -> tuple[Path, ...]:
    paths = tuple(root / name for name in relative)
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("exit 0\n", encoding="utf-8")
    return paths


def _sim_hook(command: str) -> dict:
    return {"flows": {"sim": {"pre_run_commands": [command]}}}


@pytest.mark.parametrize(
    ("document", "files", "expected"),
    [
        (
            {
                "targets": {
                    "sim": {
                        "flow_options": {
                            "tool": "verilator",
                            "verilator_options": ["--timing", "--timescale", "1ns/1ns"],
                        }
                    }
                }
            },
            (),
            (),
        ),
        (
            {
                "targets": {
                    "sim": {
                        "flow_options": {
                            "tool": "verilator",
                            "verilator_options": ["-Irtl/include"],
                            "output_dir": "generated/results",
                        }
                    }
                }
            },
            ("rtl/include/x.svh",),
            (),
        ),
        (
            {
                "targets": {
                    "sim": {
                        "flow_options": {
                            "tool": "verilator",
                            "pre_run": "python3 hooks/prepare.py --ratio 1ns/1ns",
                            "verilator_options": ["--timescale", "1ns/1ns"],
                        }
                    }
                }
            },
            ("hooks/prepare.py",),
            ("hooks/prepare.py",),
        ),
        (
            {"scripts": {"seed": {"cmd": ["python3", "hooks/seed file.py"]}}},
            ("hooks/seed file.py",),
            ("hooks/seed file.py",),
        ),
        (
            {"scripts": {"prepare": {"cmd": ["python3"], "cmd_append": ["hooks/prepare.py"]}}},
            ("hooks/prepare.py",),
            ("hooks/prepare.py",),
        ),
        (
            {
                "generators": {
                    "build": {"interpreter": "python3", "command": "generators/build.py"}
                }
            },
            ("generators/build.py",),
            ("generators/build.py",),
        ),
        (
            {
                "generators": {
                    "build": {"interpreter": "tools/python3", "command": "generators/build.py"}
                }
            },
            ("tools/python3", "generators/build.py"),
            ("generators/build.py",),
        ),
    ],
    ids=[
        "verilator-timescale",
        "slash-bearing-tool-arguments",
        "target-pre-run",
        "interpreter-script",
        "cmd-append-script",
        "generator-command",
        "generator-interpreter-path",
    ],
)
def test_core_program_paths_keep_only_declared_programs(
    tmp_path: Path, document: dict, files: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    _write(tmp_path, *files)

    assert _core_paths(tmp_path, document, strict=True) == tuple(tmp_path / p for p in expected)


@pytest.mark.parametrize(
    "document",
    [
        {"scripts": {"run": {"cmd": ["python3", "hooks/missing.py"]}}},
        {"scripts": {"run": {"cmd": ["hooks/missing"]}}},
        {"scripts": {"run": {"cmd": ["python3"], "cmd_append": ["hooks/missing.py"]}}},
        {"generators": {"run": {"command": "hooks/missing.py"}}},
    ],
)
def test_core_program_paths_reject_missing_programs(
    tmp_path: Path, document: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="referenced program is unavailable: hooks/missing"):
        _core_paths(tmp_path, document, strict=True)
    assert _core_paths(tmp_path, document) == ()


def test_core_program_paths_reject_program_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _write(tmp_path, "outside.py")

    with pytest.raises(ValueError, match="cannot be mapped to the Project"):
        _core_paths(project, {"generators": {"run": {"command": "../outside.py"}}}, strict=True)


def test_core_program_paths_include_redirecting_entries(tmp_path: Path) -> None:
    (script,) = _write(tmp_path, "real-hooks/run.py")
    link = tmp_path / "hooks"
    link.symlink_to(script.parent, target_is_directory=True)
    document = {"scripts": {"run": {"cmd": ["python3", "hooks/run.py"]}}}

    assert _core_paths(tmp_path, document, strict=True) == (link, script)


def test_core_program_paths_include_nested_repository_entry(tmp_path: Path) -> None:
    (script,) = _write(tmp_path, "vendor/generator/run.py")
    (script.parent / ".git").mkdir()
    document = {"generators": {"run": {"command": "vendor/generator/run.py"}}}

    assert _core_paths(tmp_path, document, strict=True) == (script.parent, script)


@pytest.mark.parametrize(
    ("command", "files", "expected"),
    [
        (
            "python3 hooks/build.py --output generated/report.py",
            ("hooks/build.py", "generated/report.py"),
            ("hooks/build.py",),
        ),
        ("python3 -u hooks/build.py", ("hooks/build.py",), ("hooks/build.py",)),
        ("python3 -X dev hooks/build.py", ("hooks/build.py",), ("hooks/build.py",)),
        ("python3 -Xdev hooks/build.py", ("hooks/build.py",), ("hooks/build.py",)),
        ("node build.js", ("build.js",), ("build.js",)),
        ("bash build.bash", ("build.bash",), ("build.bash",)),
        (
            "MODE=fast ./hooks/one && python3 hooks/two.py | bash hooks/three.sh",
            ("hooks/one", "hooks/two.py", "hooks/three.sh"),
            ("hooks/one", "hooks/three.sh", "hooks/two.py"),
        ),
        *(
            (f"make {arguments} all", ("build/Simulation.mk",), ("build/Simulation.mk",))
            for arguments in (
                "-f build/Simulation.mk",
                "-fbuild/Simulation.mk",
                "--file build/Simulation.mk",
                "--file=build/Simulation.mk",
                "--makefile build/Simulation.mk",
                "--makefile=build/Simulation.mk",
            )
        ),
        ('python3 -c "print("a/b")"', (), ()),
        ("python3 -m package/path", (), ()),
        ('bash -c "echo a/b"', (), ()),
        ('perl -e "print q{a/b}"', (), ()),
        ('node --eval "console.log("a/b")"', (), ()),
    ],
)
def test_project_config_program_paths_find_pre_sim_programs(
    tmp_path: Path, command: str, files: tuple[str, ...], expected: tuple[str, ...]
) -> None:
    _write(tmp_path, *files)

    paths = project_config_program_paths(_sim_hook(command), project_root=tmp_path, strict=True)

    assert paths == tuple(tmp_path / p for p in expected)


def test_project_config_program_paths_reject_malformed_command_only_in_strict_mode(
    tmp_path: Path,
) -> None:
    config = _sim_hook('python3 "hooks/build.py')

    with pytest.raises(ValueError, match="invalid executable command syntax"):
        project_config_program_paths(config, project_root=tmp_path, strict=True)
    assert project_config_program_paths(config, project_root=tmp_path) == ()


@pytest.mark.parametrize("program", ["./missing", "missing.py"])
def test_missing_declared_program_opt_in_preserves_default_and_strict(tmp_path, program):
    document = {"scripts": {"run": {"cmd": [program]}}}
    assert _core_paths(tmp_path, document) == ()
    assert _core_paths(tmp_path, document, include_missing=True) == (
        (tmp_path / program).resolve(),
    )
    with pytest.raises(ValueError, match="unavailable"):
        _core_paths(tmp_path, document, strict=True, include_missing=True)


@pytest.mark.parametrize(
    ("command", "expected"),
    [("missing", ("missing",)), ("nested/missing", ("nested", "nested/missing"))],
    ids=["bare-generator-program", "redirecting-parent"],
)
def test_missing_generator_program_is_protected(tmp_path, command, expected):
    (tmp_path / "nested" / ".git").mkdir(parents=True)
    document = {"generators": {"gen": {"command": command}}}
    assert _core_paths(tmp_path, document, include_missing=True) == tuple(
        tmp_path / p for p in expected
    )


def _pre_sim_project(tmp_path: Path, *, linked: bool) -> tuple[Path, Path]:
    """A Project whose Pre-Sim Command runs ``.booley/hooks/x.sh``.

    With *linked*, ``.booley`` is a symlink to a data directory beside the
    Project Root, as in a GitHub Codespace. Returns ``(project, data)``.
    """
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "booley-data" if linked else project / ".booley"
    _write(data, "hooks/x.sh")
    # A Versioned Booley Directory is a nested repository in the plain layout too.
    (data / ".git").mkdir()
    if linked:
        symlink_or_skip(project / ".booley", data, target_is_directory=True)
    return project, data


@pytest.mark.parametrize("linked", [False, True], ids=["plain", "linked"])
def test_pre_sim_program_under_booley_dir_entry_is_kept(tmp_path: Path, linked: bool) -> None:
    """A linked ``.booley`` entry yields the same labels as a plain directory (#2250)."""
    project, _data = _pre_sim_project(tmp_path, linked=linked)

    paths = project_config_program_paths(
        _sim_hook("bash .booley/hooks/x.sh"), project_root=project, strict=True
    )

    assert [path.relative_to(project).as_posix() for path in paths] == [
        ".booley",
        ".booley/hooks/x.sh",
    ]


@pytest.mark.parametrize("through", ["below-linked-entry", "above-entry-spelling"])
def test_link_out_of_the_project_stays_excluded(tmp_path: Path, through: str) -> None:
    """Only the exact ``.booley`` entry is followed, not a link below it or above it."""
    project, data = _pre_sim_project(tmp_path, linked=True)
    elsewhere = tmp_path / "elsewhere"
    if through == "below-linked-entry":
        _write(elsewhere, "y.sh")
        symlink_or_skip(data / "escape", elsewhere, target_is_directory=True)
        config = _sim_hook("bash .booley/escape/y.sh")
    else:
        _write(elsewhere, ".booley/hooks/z.sh")
        symlink_or_skip(project / "vendor", elsewhere, target_is_directory=True)
        config = _sim_hook("bash vendor/.booley/hooks/z.sh")

    assert project_config_program_paths(config, project_root=project) == ()
    with pytest.raises(ValueError, match="cannot be mapped to the Project"):
        project_config_program_paths(config, project_root=project, strict=True)
