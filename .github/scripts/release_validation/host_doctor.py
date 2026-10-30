"""Exercise Host Bootstrap, Project Initialization, and plain Doctor in isolation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

_MCP_TOOLS = re.compile(r"MCP server exposes [0-9]+ MCP tool\(s\)")
# Doctor's closing counts row for failures, e.g. "  ✗ failed     0".
_FAILED_COUNT = re.compile(r"^\s*\S*\s*failed\s+(\d+)$", re.MULTILINE)
# Color codes a FORCE_COLOR environment adds around Doctor's rows.
_COLOR_CODES = re.compile(r"\x1b\[[0-9;]*m")
# Status markers Project Initialization prints for a warning or a failure.
_PROBLEM_MARKERS = ("[!!]", "[XX]")
# Executables stubbed into the isolated home's bin for the duration of a run.
_EDITOR_PROBE = "code"
_AGENT_CLI_PROBE = "codex"


def doctor_reports_no_failures(output: str) -> bool:
    """True when Doctor's counts table says ``failed`` is 0 (color codes ignored)."""
    match = _FAILED_COUNT.search(_COLOR_CODES.sub("", output))
    return match is not None and match.group(1) == "0"


def _inside(root: Path, path: Path, label: str) -> Path:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"{label} must be inside allowed root")
    return resolved


def _run(command: list[str], *, project: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        command,
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    output = result.stdout + result.stderr
    if result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {command!r}\n{output}")
    return output


def _environment(home: Path, executable: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{executable.parent}{os.pathsep}{home / 'bin'}{os.pathsep}{env['PATH']}",
            "PYTHONUSERBASE": str(home / ".local"),
            "XDG_CONFIG_HOME": str(home / ".config"),
        }
    )
    return env


def _write_probe(home: Path, name: str, script: str) -> Path:
    """Write an executable stub into the isolated home's ``bin``; refuse to overwrite."""
    probe = home / "bin" / name
    probe.parent.mkdir(parents=True, exist_ok=True)
    if probe.exists() or probe.is_symlink():
        raise ValueError(f"isolated {name} probe path already exists")
    probe.write_text(script, encoding="utf-8")
    probe.chmod(0o755)
    return probe


def _prepare_editor_probe(home: Path) -> Path:
    return _write_probe(
        home,
        _EDITOR_PROBE,
        "#!/bin/sh\n"
        "set -eu\n"
        'marker="${HOME}/.booley-ci-dev-containers"\n'
        'case "${1:-}" in\n'
        "  --version) printf '1.0.0\\n' ;;\n"
        "  --list-extensions)\n"
        '    if test -e "${marker}"; then '
        "printf 'ms-vscode-remote.remote-containers\\n'; fi ;;\n"
        "  --install-extension)\n"
        '    test "${2:-}" = ms-vscode-remote.remote-containers\n'
        '    : > "${marker}" ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n",
    )


def _prepare_agent_cli_probe(home: Path) -> Path:
    """Stub the host agent CLI that Host Bootstrap's ``host-agent-cli`` check looks for.

    A fresh runner has neither ``claude`` nor ``codex``, so without the stub
    Project Initialization prints that (correct) prerequisite warning and the
    clean-initialization gate fails. The Project selects ``codex``.
    """
    return _write_probe(
        home,
        _AGENT_CLI_PROBE,
        "#!/bin/sh\n"
        'case "${1:-}" in\n'
        "  --version) printf 'codex-cli 0.0.0-release-validation\\n' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n",
    )


def _problem_lines(output: str) -> list[str]:
    """Return the output lines that carry a warning or failure marker."""
    return [line for line in output.splitlines() if any(m in line for m in _PROBLEM_MARKERS)]


def _editor_marker(home: Path) -> Path:
    return home / ".booley-ci-dev-containers"


def validate(
    *,
    allowed_root: Path,
    project: Path,
    home: Path,
    booley: Path,
    expected_uid: int,
    expected_gid: int,
    candidate_sha: str,
    image_digest: str,
) -> dict[str, object]:
    project = _inside(allowed_root, project, "project")
    home = _inside(allowed_root, home, "home")
    booley = _inside(allowed_root, booley, "booley")
    identity = {"uid": os.getuid(), "gid": os.getgid()}
    if identity != {"uid": expected_uid, "gid": expected_gid}:
        raise RuntimeError(f"host identity differs: {identity}")
    # Track each stub as it is created so cleanup never removes a file this run did not write.
    probes: list[Path] = []
    checks = [{"id": "host.identity", "status": "pass"}]
    try:
        probes.append(_prepare_editor_probe(home))
        probes.append(_prepare_agent_cli_probe(home))
        env = _environment(home, booley)
        _run(["git", "init", "-b", "main"], project=project, env=env)
        checks.append({"id": "project.repository", "status": "pass"})
        _run([str(booley), "bootstrap"], project=project, env=env)
        checks.append({"id": "host-bootstrap", "status": "pass"})
        init = _run(
            [
                str(booley),
                "init",
                "--scaffold",
                "release_host_doctor",
                "--sim-eda-tool",
                "verilator",
                "--tb-style",
                "sv",
                "--lint-eda-tool",
                "verilator",
                "--no-asic",
                "--provider",
                "codex",
                # The runner has no agent credential. A pinned policy without its
                # credential is a Doctor failure by design, so keep the default.
                "--auth",
                "auto",
                "--skip-credentials",
            ],
            project=project,
            env=env,
        )
        problems = _problem_lines(init)
        if problems:
            raise RuntimeError(
                "Project Initialization reported a warning or failure:\n" + "\n".join(problems)
            )
        checks.append({"id": "project-initialization.clean", "status": "pass"})
        doctor = _run([str(booley), "doctor"], project=project, env=env)
        if not doctor_reports_no_failures(doctor) or _MCP_TOOLS.search(doctor) is None:
            raise RuntimeError("plain host Doctor did not prove the issued-image MCP seam")
        checks.append({"id": "host-doctor.plain-issued-image", "status": "pass"})
    finally:
        for probe in probes:
            probe.unlink(missing_ok=True)
        _editor_marker(home).unlink(missing_ok=True)
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": identity,
        "checks": checks,
        "cleanup": {
            "editor_probe_removed": not (home / "bin" / _EDITOR_PROBE).exists(),
            "agent_cli_probe_removed": not (home / "bin" / _AGENT_CLI_PROBE).exists(),
            "editor_marker_removed": not _editor_marker(home).exists(),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allowed-root", required=True, type=Path)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--booley", required=True, type=Path)
    parser.add_argument("--expected-uid", type=int, default=1000)
    parser.add_argument("--expected-gid", type=int, required=True)
    parser.add_argument("--candidate-sha", default=os.environ.get("GITHUB_SHA", "unknown"))
    parser.add_argument("--image-digest", required=True)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    evidence = validate(
        allowed_root=args.allowed_root,
        project=args.project,
        home=args.home,
        booley=args.booley,
        expected_uid=args.expected_uid,
        expected_gid=args.expected_gid,
        candidate_sha=args.candidate_sha,
        image_digest=args.image_digest,
    )
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
