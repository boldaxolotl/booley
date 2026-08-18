#!/bin/bash
# Red-team isolation tests for booley-sandbox Docker container.
# Verifies that a sandboxed agent CANNOT escape, exfiltrate, or DoS the host.
#
# Usage: ./test_isolation.sh [--verbose]
#
# Each test runs a command inside the container and asserts it fails.
# Exit 0 = all isolation checks pass.  Exit 1 = a test failed (sandbox broken).
set -euo pipefail

IMAGE="booley-sandbox"
VERBOSE="${1:-}"
PASS=0
FAIL=0
SKIP=0

# ─── helpers ──────────────────────────────────────────────────────────────────

green()  { printf "\033[32m✓ %s\033[0m\n" "$1"; }
red()    { printf "\033[31m✗ %s\033[0m\n" "$1"; }
yellow() { printf "\033[33m⊘ %s\033[0m\n" "$1"; }

# Run a command inside a fresh container that mirrors pipeline settings.
# Uses the same flags as DockerRunner._build_docker_cmd.
sandbox_run() {
    docker run --rm --init \
        --network bridge \
        --user agent \
        --memory 512m \
        --pids-limit 256 \
        "$IMAGE" \
        bash -c "$1" 2>&1
}

# Assert command FAILS (non-zero exit or "permission denied" / "not found")
assert_fails() {
    local desc="$1"
    local cmd="$2"
    local output
    local rc=0
    output=$(sandbox_run "$cmd") || rc=$?

    if [ $rc -ne 0 ]; then
        green "$desc"
        [ "$VERBOSE" = "--verbose" ] && echo "    ↳ exit=$rc: ${output:0:120}"
        ((PASS++))
    else
        # Some commands "succeed" but produce empty/harmless output — check
        if echo "$output" | grep -qiE "permission denied|operation not permitted|read-only|no such file"; then
            green "$desc (blocked by kernel)"
            ((PASS++))
        else
            red "$desc"
            echo "    ↳ UNEXPECTED SUCCESS: exit=$rc output=${output:0:200}"
            ((FAIL++))
        fi
    fi
}

# Assert command SUCCEEDS (confirms the sandbox works normally)
assert_succeeds() {
    local desc="$1"
    local cmd="$2"
    local output
    local rc=0
    output=$(sandbox_run "$cmd") || rc=$?

    if [ $rc -eq 0 ]; then
        green "$desc"
        ((PASS++))
    else
        red "$desc"
        echo "    ↳ UNEXPECTED FAILURE: exit=$rc output=${output:0:200}"
        ((FAIL++))
    fi
}

# ─── pre-flight ───────────────────────────────────────────────────────────────

echo "╔══════════════════════════════════════════════════════╗"
echo "║  Booley Sandbox Isolation Tests                     ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

if ! docker image inspect "$IMAGE" &>/dev/null; then
    echo "ERROR: Image '$IMAGE' not found. Run src/booley/data/docker/build.sh first."
    exit 1
fi

# Create a canary file on the host to test filesystem escape
CANARY_DIR=$(mktemp -d)
echo "HOST_SECRET_$(date +%s)" > "$CANARY_DIR/canary.txt"
CANARY_CONTENT=$(cat "$CANARY_DIR/canary.txt")
trap "rm -rf '$CANARY_DIR'" EXIT

# ─── 1. FILESYSTEM ISOLATION ─────────────────────────────────────────────────

echo ""
echo "── 1. Filesystem Isolation ──"

assert_fails "Cannot read /etc/shadow" \
    "cat /etc/shadow"

assert_fails "Cannot write to /usr" \
    "touch /usr/PWNED"

assert_fails "Cannot write to /etc" \
    "touch /etc/PWNED"

assert_fails "Cannot write outside /work and /home/agent" \
    "touch /tmp/../PWNED && touch /var/PWNED && touch /opt/PWNED"

assert_fails "Cannot access Docker socket" \
    "ls /var/run/docker.sock || curl --unix-socket /var/run/docker.sock http://localhost/info"

assert_fails "Cannot mount host filesystems" \
    "mount -t proc proc /mnt 2>&1 || mount --bind / /mnt 2>&1"

assert_succeeds "CAN write to /work (bind-mount target)" \
    "touch /work/test_file_ok && rm /work/test_file_ok"

assert_succeeds "CAN write to /home/agent" \
    "touch /home/agent/test_file_ok && rm /home/agent/test_file_ok"


# ─── 2. PROCESS / NAMESPACE ISOLATION ────────────────────────────────────────

echo ""
echo "── 2. Process / Namespace Isolation ──"

assert_fails "Cannot see host processes (PID namespace)" \
    "ps aux | grep -v 'grep' | grep -c '' | awk '{if (\$1 > 10) exit 0; else exit 1}'"

assert_fails "Cannot ptrace other processes" \
    "apt-get install -y strace 2>/dev/null; strace -p 1 2>&1"

assert_fails "Cannot escalate to root" \
    "sudo whoami 2>&1 && su -c whoami 2>&1"

assert_fails "Cannot use nsenter" \
    "nsenter --target 1 --mount --uts --ipc --net --pid -- whoami 2>&1"

assert_fails "Cannot load kernel modules" \
    "insmod /dev/null 2>&1 || modprobe test 2>&1"

# ─── 3. NETWORK ISOLATION ────────────────────────────────────────────────────

echo ""
echo "── 3. Network Isolation ──"

# With --network=bridge (default), container has internet but NOT host-network access
# NOTE: If you switch to --network=none, update these tests

assert_fails "Cannot access host Docker API on default bridge" \
    "curl -s --connect-timeout 3 http://host.docker.internal:2375/info"

assert_fails "Cannot access common host services (22/SSH)" \
    "curl -s --connect-timeout 3 http://host.docker.internal:22 || nc -z -w3 host.docker.internal 22"

# This SHOULD fail if you configure --network=none; passes with bridge
# Uncomment the next test if you switch to --network=none:
# assert_fails "Cannot reach external network" \
#     "curl -s --connect-timeout 5 https://httpbin.org/get"


# ─── 4. RESOURCE LIMITS (DoS protection) ─────────────────────────────────────

echo ""
echo "── 4. Resource Limits (DoS Protection) ──"

# Fork bomb — should be killed by --pids-limit
assert_fails "Fork bomb is killed by pids-limit" \
    ":(){ :|:& };: ; sleep 2"

# Memory bomb — should be OOM-killed by --memory limit
assert_fails "Memory bomb is OOM-killed" \
    "python3 -c \"x = []; [x.append(b'A' * 10**8) for _ in range(100)]\" 2>&1"

# Disk fill — limited to container writable layer (no host impact)
assert_fails "Cannot fill disk beyond container overlay limit" \
    "dd if=/dev/zero of=/tmp/bomb bs=1M count=2048 2>&1"


# ─── 5. CREDENTIAL ISOLATION ─────────────────────────────────────────────────

echo ""
echo "── 5. Credential Isolation ──"

# Auth files are mounted read-only — agent cannot modify or exfiltrate beyond API use
assert_fails "Cannot modify mounted auth file" \
    "echo PWNED > /home/agent/.claude/.credentials.json 2>&1"

assert_fails "Cannot read other users' home dirs" \
    "ls /root/ 2>&1 && cat /root/.bashrc 2>&1"

assert_fails "Cannot access host SSH keys" \
    "ls /home/agent/.ssh/ 2>&1 && cat /root/.ssh/id_rsa 2>&1"

# ─── 6. CONTAINER ESCAPE VECTORS ─────────────────────────────────────────────

echo ""
echo "── 6. Container Escape Vectors ──"

assert_fails "Cannot access /proc/sysrq-trigger" \
    "echo b > /proc/sysrq-trigger 2>&1"

assert_fails "Cannot write to /proc/self/exe" \
    "cp /bin/bash /proc/self/exe 2>&1"

assert_fails "Cannot use CAP_SYS_ADMIN (unshare)" \
    "unshare --user --map-root-user whoami 2>&1"

assert_fails "Cannot access host kernel params via /proc/sys" \
    "echo 1 > /proc/sys/net/ipv4/ip_forward 2>&1"

assert_fails "Cannot create device nodes" \
    "mknod /tmp/sda b 8 0 2>&1"

assert_fails "Cannot use chroot escape" \
    "mkdir -p /tmp/escape && chroot /tmp/escape /bin/sh -c 'echo escaped' 2>&1"


# ─── SUMMARY ─────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ $FAIL -gt 0 ]; then
    echo ""
    red "SANDBOX ISOLATION BROKEN — $FAIL test(s) failed!"
    exit 1
fi

green "All isolation checks passed — sandbox is secure."
exit 0
