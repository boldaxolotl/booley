"""Exercise Host Bootstrap, Project Initialization, and deep Doctor in isolation."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

_MCP_TOOLS = re.compile(r"MCP server exposes [0-9]+ MCP tool\(s\)")


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
        }
    )
    return env


def _prepare_editor_probe(home: Path) -> Path:
    probe = home / "bin" / "code"
    probe.parent.mkdir(parents=True, exist_ok=True)
    if probe.exists() or probe.is_symlink():
        raise ValueError("isolated editor probe path already exists")
    probe.write_text(
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
        encoding="utf-8",
    )
    probe.chmod(0o755)
    return probe


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
    probe = _prepare_editor_probe(home)
    checks = [{"id": "host.identity", "status": "pass"}]
    try:
        env = _environment(home, booley)
        _run([str(booley), "bootstrap"], project=project, env=env)
        checks.append({"id": "host-bootstrap", "status": "pass"})
        init = _run([str(booley), "init", "--skip-credentials"], project=project, env=env)
        if "[!!]" in init or "[XX]" in init:
            raise RuntimeError("Project Initialization reported a warning or failure")
        checks.append({"id": "project-initialization.clean", "status": "pass"})
        doctor = _run(
            [str(booley), "doctor", "--deep", "--skip-agent-checks"],
            project=project,
            env=env,
        )
        if "0 failed." not in doctor or _MCP_TOOLS.search(doctor) is None:
            raise RuntimeError("deep Doctor did not prove the issued-image MCP seam")
        checks.append({"id": "host-doctor.deep-issued-image", "status": "pass"})
    finally:
        probe.unlink(missing_ok=True)
        _editor_marker(home).unlink(missing_ok=True)
    return {
        "schema": 1,
        "candidate": {"sha": candidate_sha, "image_digest": image_digest},
        "identity": identity,
        "checks": checks,
        "cleanup": {"editor_probe_removed": not probe.exists()},
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
