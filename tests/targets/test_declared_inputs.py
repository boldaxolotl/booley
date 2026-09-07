"""Schema-aware discovery of Project-controlled program inputs."""

from pathlib import Path

import pytest

from booley.targets.declared_inputs import core_program_paths, project_config_program_paths


def test_core_program_paths_ignore_verilator_timescale(tmp_path: Path) -> None:
    core = tmp_path / "toy.core"
    document = {
        "targets": {
            "sim": {
                "flow": "sim",
                "flow_options": {
                    "tool": "verilator",
                    "verilator_options": ["--timing", "--timescale", "1ns/1ns"],
                },
            }
        }
    }

    assert core_program_paths(document, core_file=core, project_root=tmp_path, strict=True) == ()


def test_core_program_paths_ignore_slash_bearing_tool_arguments(tmp_path: Path) -> None:
    (tmp_path / "rtl" / "include").mkdir(parents=True)
    document = {
        "targets": {
            "sim": {
                "flow_options": {
                    "tool": "verilator",
                    "verilator_options": ["-Irtl/include"],
                    "output_dir": "generated/results",
                }
            }
        }
    }

    assert (
        core_program_paths(
            document,
            core_file=tmp_path / "toy.core",
            project_root=tmp_path,
            strict=True,
        )
        == ()
    )


def test_core_program_paths_include_explicit_target_pre_run_program(tmp_path: Path) -> None:
    script = tmp_path / "hooks" / "prepare.py"
    script.parent.mkdir()
    script.write_text("print('prepare')\n", encoding="utf-8")
    document = {
        "targets": {
            "sim": {
                "flow_options": {
                    "tool": "verilator",
                    "pre_run": "python3 hooks/prepare.py --ratio 1ns/1ns",
                    "verilator_options": ["--timescale", "1ns/1ns"],
                }
            }
        }
    }

    assert core_program_paths(
        document,
        core_file=tmp_path / "toy.core",
        project_root=tmp_path,
        strict=True,
    ) == (script,)


def test_core_program_paths_include_interpreter_script(tmp_path: Path) -> None:
    script = tmp_path / "hooks" / "seed file.py"
    script.parent.mkdir()
    script.write_text("print('seed')\n", encoding="utf-8")
    document = {"scripts": {"seed": {"cmd": ["python3", "hooks/seed file.py"]}}}

    assert core_program_paths(
        document,
        core_file=tmp_path / "toy.core",
        project_root=tmp_path,
        strict=True,
    ) == (script,)


def test_core_program_paths_include_generator_command(tmp_path: Path) -> None:
    generator = tmp_path / "generators" / "build.py"
    generator.parent.mkdir()
    generator.write_text("print('build')\n", encoding="utf-8")
    document = {
        "generators": {"build": {"interpreter": "python3", "command": "generators/build.py"}}
    }

    assert core_program_paths(
        document,
        core_file=tmp_path / "toy.core",
        project_root=tmp_path,
        strict=True,
    ) == (generator,)


def test_project_config_program_paths_include_only_pre_run_program(tmp_path: Path) -> None:
    script = tmp_path / "hooks" / "build.py"
    script.parent.mkdir()
    script.write_text("print('build')\n", encoding="utf-8")
    output = tmp_path / "generated" / "report.py"
    output.parent.mkdir()
    output.write_text("result\n", encoding="utf-8")
    config = {
        "flows": {
            "sim": {
                "pre_run_commands": ["python3 hooks/build.py --output generated/report.py"],
                "run_cwd": "tests/work",
                "output_dir": "generated/results",
            }
        }
    }

    assert project_config_program_paths(config, project_root=tmp_path, strict=True) == (script,)


def test_project_config_program_paths_support_safe_interpreter_flags(tmp_path: Path) -> None:
    script = tmp_path / "hooks" / "build.py"
    script.parent.mkdir()
    script.write_text("print('build')\n", encoding="utf-8")
    config = {"flows": {"sim": {"pre_run_commands": ["python3 -u hooks/build.py"]}}}

    assert project_config_program_paths(config, project_root=tmp_path, strict=True) == (script,)


@pytest.mark.parametrize(
    ("command", "filename"),
    [("node build.js", "build.js"), ("bash build.bash", "build.bash")],
)
def test_project_config_program_paths_support_interpreter_script_suffixes(
    tmp_path: Path, command: str, filename: str
) -> None:
    script = tmp_path / filename
    script.write_text("script\n", encoding="utf-8")
    config = {"flows": {"sim": {"pre_run_commands": [command]}}}

    assert project_config_program_paths(config, project_root=tmp_path, strict=True) == (script,)


def test_project_config_program_paths_find_separated_commands_after_assignments(
    tmp_path: Path,
) -> None:
    scripts = tuple(tmp_path / "hooks" / name for name in ("one", "two.py", "three.sh"))
    scripts[0].parent.mkdir()
    for script in scripts:
        script.write_text("exit 0\n", encoding="utf-8")
    config = {
        "flows": {
            "sim": {
                "pre_run_commands": [
                    "MODE=fast ./hooks/one && python3 hooks/two.py | bash hooks/three.sh"
                ]
            }
        }
    }

    assert project_config_program_paths(config, project_root=tmp_path, strict=True) == tuple(
        sorted(scripts)
    )


@pytest.mark.parametrize(
    "arguments",
    [
        "-f build/Simulation.mk",
        "-fbuild/Simulation.mk",
        "--file build/Simulation.mk",
        "--file=build/Simulation.mk",
        "--makefile build/Simulation.mk",
        "--makefile=build/Simulation.mk",
    ],
)
def test_project_config_program_paths_include_explicit_makefile(
    tmp_path: Path, arguments: str
) -> None:
    makefile = tmp_path / "build" / "Simulation.mk"
    makefile.parent.mkdir()
    makefile.write_text("all:\n\t@true\n", encoding="utf-8")
    config = {"flows": {"sim": {"pre_run_commands": [f"make {arguments} all"]}}}

    assert project_config_program_paths(config, project_root=tmp_path, strict=True) == (makefile,)


@pytest.mark.parametrize(
    "command",
    [
        'python3 -c "print("a/b")"',
        "python3 -m package/path",
        'bash -c "echo a/b"',
        'perl -e "print q{a/b}"',
        'node --eval "console.log("a/b")"',
    ],
)
def test_project_config_program_paths_ignore_interpreter_non_script_modes(
    tmp_path: Path, command: str
) -> None:
    config = {"flows": {"sim": {"pre_run_commands": [command]}}}

    assert project_config_program_paths(config, project_root=tmp_path, strict=True) == ()


@pytest.mark.parametrize(
    "document",
    [
        {"scripts": {"run": {"cmd": ["python3", "hooks/missing.py"]}}},
        {"scripts": {"run": {"cmd": ["hooks/missing"]}}},
        {"generators": {"run": {"command": "hooks/missing.py"}}},
    ],
)
def test_core_program_paths_reject_missing_programs(
    tmp_path: Path, document: dict[str, object]
) -> None:
    with pytest.raises(ValueError, match="referenced program is unavailable: hooks/missing"):
        core_program_paths(
            document,
            core_file=tmp_path / "toy.core",
            project_root=tmp_path,
            strict=True,
        )


def test_core_program_paths_reject_program_outside_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    document = {"generators": {"run": {"command": "../outside.py"}}}

    with pytest.raises(ValueError, match="cannot be mapped to the Project"):
        core_program_paths(
            document,
            core_file=project / "toy.core",
            project_root=project,
            strict=True,
        )


def test_core_program_paths_include_redirecting_entries(tmp_path: Path) -> None:
    real = tmp_path / "real-hooks"
    real.mkdir()
    script = real / "run.py"
    script.write_text("print('run')\n", encoding="utf-8")
    link = tmp_path / "hooks"
    link.symlink_to(real, target_is_directory=True)
    document = {"scripts": {"run": {"cmd": ["python3", "hooks/run.py"]}}}

    assert core_program_paths(
        document,
        core_file=tmp_path / "toy.core",
        project_root=tmp_path,
        strict=True,
    ) == (link, script)


def test_core_program_paths_include_nested_repository_entry(tmp_path: Path) -> None:
    nested = tmp_path / "vendor" / "generator"
    nested.mkdir(parents=True)
    (nested / ".git").mkdir()
    script = nested / "run.py"
    script.write_text("print('run')\n", encoding="utf-8")
    document = {"generators": {"run": {"command": "vendor/generator/run.py"}}}

    assert core_program_paths(
        document,
        core_file=tmp_path / "toy.core",
        project_root=tmp_path,
        strict=True,
    ) == (nested, script)


def test_core_program_paths_ignore_generator_interpreter_path(tmp_path: Path) -> None:
    interpreter = tmp_path / "tools" / "python3"
    interpreter.parent.mkdir()
    interpreter.write_text("#!/bin/sh\n", encoding="utf-8")
    generator = tmp_path / "generators" / "build.py"
    generator.parent.mkdir()
    generator.write_text("print('build')\n", encoding="utf-8")
    document = {
        "generators": {
            "build": {
                "interpreter": "tools/python3",
                "command": "generators/build.py",
            }
        }
    }

    assert core_program_paths(
        document,
        core_file=tmp_path / "toy.core",
        project_root=tmp_path,
        strict=True,
    ) == (generator,)


def test_core_program_paths_omit_missing_program_when_not_strict(tmp_path: Path) -> None:
    document = {"generators": {"run": {"command": "hooks/missing.py"}}}

    assert (
        core_program_paths(
            document,
            core_file=tmp_path / "toy.core",
            project_root=tmp_path,
        )
        == ()
    )
