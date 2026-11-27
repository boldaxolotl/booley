#!/usr/bin/env bash
# Run a network command; retry a failure up to 8 times, 30 s apart (#4420).
set -u
for attempt in 1 2 3 4 5 6 7 8 9; do
  "$@" && exit 0
  status=$?
  if (( attempt == 9 )); then exit "${status}"; fi
  echo "::warning::$1 failed (exit ${status}, attempt ${attempt} of 9); retrying in 30 s" >&2
  sleep 30
done
