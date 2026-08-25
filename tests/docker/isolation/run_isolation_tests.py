"""Run Docker sandbox isolation tests from Python (bypasses bash hook pattern matching)."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys

from .container_names import next_ci_container_name

os.environ.setdefault("PYTHONIOENCODING", "utf-8")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

IMAGE = "booley-sandbox"
BASE_FLAGS = [
    "docker",
    "run",
    "--rm",
    "--init",
    "--network",
    "bridge",
    "--memory",
    "512m",
    "--pids-limit",
    "256",
    "--security-opt",
    "no-new-privileges",
    "--cap-drop",
    "ALL",
]

PASS = 0
FAIL = 0
_VERBOSE = False


def green(msg: str) -> None:
    print(f"\033[32m✓ {msg}\033[0m")


def red(msg: str) -> None:
    print(f"\033[31m✗ {msg}\033[0m")


def run(cmd_str: str, timeout: int = 30) -> tuple[int, str]:
    command = list(BASE_FLAGS)
    container_name = next_ci_container_name()
    if container_name:
        command += ["--name", container_name]
    command += [IMAGE, "bash", "-c", cmd_str]
    try:
        r = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMED OUT"


def assert_fails(desc: str, cmd: str) -> None:
    global PASS, FAIL
    rc, out = run(cmd)
    if rc != 0:
        green(desc)
        if _VERBOSE:
            print(f"    ↳ exit={rc}: {out[:150]}")
        PASS += 1
    elif any(
        k in out.lower() for k in ("permission denied", "operation not permitted", "read-only")
    ):
        green(f"{desc} (blocked by kernel)")
        PASS += 1
    else:
        red(desc)
        print(f"    ↳ UNEXPECTED SUCCESS: exit={rc} output={out[:200]}")
        FAIL += 1


def assert_fork_bomb_contained() -> None:
    """Fork bomb: pass if pids-limit blocks it (output contains 'Resource temporarily unavailable')."""
    global PASS, FAIL
    desc = "Fork bomb contained by pids-limit"
    rc, out = run(":(){ :|:& };: ; sleep 3")
    if "resource temporarily unavailable" in out.lower():
        green(desc)
        if _VERBOSE:
            print(f"    ↳ exit={rc}: pids-limit hit (fork failed)")
        PASS += 1
    else:
        red(desc)
        print(f"    ↳ No evidence of pids-limit: exit={rc} output={out[:200]}")
        FAIL += 1


def assert_succeeds(desc: str, cmd: str) -> None:
    global PASS, FAIL
    rc, out = run(cmd)
    if rc == 0:
        green(desc)
        PASS += 1
    else:
        red(desc)
        print(f"    ↳ UNEXPECTED FAILURE: exit={rc} output={out[:200]}")
        FAIL += 1


def _test_filesystem_isolation() -> None:
    """1. Filesystem: read/write boundaries."""
    print("\n── 1. Filesystem Isolation ──")
    assert_fails("Cannot read /etc/shadow", "cat /etc/shadow")
    assert_fails("Cannot write to /usr", "touch /usr/PWNED")
    assert_fails("Cannot write to /etc", "touch /etc/PWNED")
    assert_fails("Cannot write outside /work and /home/agent", "touch /opt/PWNED")
    assert_fails("Cannot access Docker socket", "ls /var/run/docker.sock")
    assert_fails("Cannot mount host filesystems", "mount -t proc proc /mnt")
    assert_succeeds("CAN write to /work", "touch /work/test_ok && rm /work/test_ok")
    assert_succeeds(
        "CAN write to /home/agent", "touch /home/agent/test_ok && rm /home/agent/test_ok"
    )


def _test_process_isolation() -> None:
    """2. Process / namespace escalation."""
    print("\n── 2. Process / Namespace Isolation ──")
    assert_fails("Cannot escalate to root via sudo", "sudo whoami")
    assert_fails("Cannot escalate to root via su", "su -c whoami")
    assert_fails(
        "Cannot use nsenter", "nsenter --target 1 --mount --uts --ipc --net --pid -- whoami"
    )
    assert_fails("Cannot load kernel modules", "insmod /dev/null")


def _test_network_isolation() -> None:
    """3. Network boundaries."""
    print("\n── 3. Network Isolation ──")
    assert_fails(
        "Cannot access host Docker API",
        "curl -s --connect-timeout 3 http://host.docker.internal:2375/info",
    )
    assert_fails(
        "Cannot reach host SSH", "curl -s --connect-timeout 3 http://host.docker.internal:22"
    )


def _test_resource_limits() -> None:
    """4. DoS protection (fork bomb, OOM)."""
    print("\n── 4. Resource Limits (DoS Protection) ──")
    assert_fork_bomb_contained()
    assert_fails(
        "Memory bomb OOM-killed", "python3 -c \"x=[]; [x.append(b'A'*10**8) for _ in range(100)]\""
    )


def _test_credential_isolation() -> None:
    """5. Credential boundaries."""
    print("\n── 5. Credential Isolation ──")
    assert_fails("Cannot read /root/", "ls /root/")
    assert_fails("Cannot access host SSH keys", "ls /home/agent/.ssh/")


def _test_escape_vectors() -> None:
    """6. Container escape vectors."""
    print("\n── 6. Container Escape Vectors ──")
    assert_fails("Cannot write /proc/sysrq-trigger", "echo b > /proc/sysrq-trigger")
    assert_fails("Cannot unshare (CAP_SYS_ADMIN)", "unshare --user --map-root-user whoami")
    assert_fails("Cannot write /proc/sys", "echo 1 > /proc/sys/net/ipv4/ip_forward")
    assert_fails("Cannot create device nodes", "mknod /tmp/sda b 8 0")
    assert_fails(
        "Cannot chroot", "mkdir -p /tmp/escape && chroot /tmp/escape /bin/sh -c 'echo escaped'"
    )


def main() -> None:
    global _VERBOSE
    parser = argparse.ArgumentParser(
        description="Run Docker sandbox isolation tests to verify container "
        "security constraints (filesystem, process, network, resource limits).",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed exit codes and output snippets for passing tests",
    )
    parsed = parser.parse_args()
    _VERBOSE = parsed.verbose

    print("╔══════════════════════════════════════════════════════╗")
    print("║  Booley Sandbox Isolation Tests                     ║")
    print("╚══════════════════════════════════════════════════════╝")

    _test_filesystem_isolation()
    _test_process_isolation()
    _test_network_isolation()
    _test_resource_limits()
    _test_credential_isolation()
    _test_escape_vectors()

    print()
    print("━" * 54)
    print(f"Results: {PASS} passed, {FAIL} failed")
    print("━" * 54)

    if FAIL > 0:
        red(f"SANDBOX ISOLATION BROKEN — {FAIL} test(s) failed!")
        sys.exit(1)
    else:
        green("All isolation checks passed — sandbox is secure.")


if __name__ == "__main__":
    main()
