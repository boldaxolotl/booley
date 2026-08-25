#!/usr/bin/env bash
set -euo pipefail

if (( $# < 2 )); then
  echo "usage: $0 CONTAINER_NAME_PREFIX COMMAND [ARG ...]" >&2
  exit 2
fi

container_prefix="$1"
shift
if [[ ! "$container_prefix" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ]]; then
  echo "invalid Docker container name prefix: $container_prefix" >&2
  exit 2
fi

cleanup() {
  local containers=()
  mapfile -t containers < <(
    docker ps -aq --filter "name=^/${container_prefix}" 2>/dev/null || true
  )
  if (( ${#containers[@]} )); then
    docker rm -f "${containers[@]}" >/dev/null 2>&1 || true
  fi
}

child_pid=""
terminate() {
  local signal="$1"
  local status="$2"
  trap - INT TERM EXIT
  if [[ -n "$child_pid" ]] && kill -0 "$child_pid" 2>/dev/null; then
    kill -s "$signal" -- "-$child_pid" 2>/dev/null || true
    kill -s KILL -- "-$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  cleanup
  exit "$status"
}

# A foreground synchronous wait defers Bash traps. Run the validation in its
# own process group and use the interruptible `wait` builtin so cancellation
# can reclaim both the complete child tree and daemon-owned containers.
trap 'terminate INT 130' INT
trap 'terminate TERM 143' TERM
trap cleanup EXIT

setsid -- "$@" &
child_pid=$!
set +e
wait "$child_pid"
status=$?
set -e
exit "$status"
