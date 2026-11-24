#!/usr/bin/env bash
# Run in an isolated environment containing the candidate compiler, GTKWave,
# Xvfb, and xauth. Retain version, source identity, FST, and reader diagnostics.
set -euo pipefail
# Hang guard of tests/time_budget.py: 30 s times BOOLEY_TEST_TIME_SCALE (unset: 1).
factor="${BOOLEY_TEST_TIME_SCALE-1}"
if [[ ! "$factor" =~ ^\+?([0-9]+\.?[0-9]*|\.[0-9]+)([eE][-+]?[0-9]+)?$ ]]; then
  echo "BOOLEY_TEST_TIME_SCALE=$factor is not a positive finite number" >&2
  exit 1
fi
guard="$(awk -v f="$factor" \
  'BEGIN { g = 30 * f; if (!(g > 0) || g == g + 1) exit 1; print (g == int(g)) ? g : int(g) + 1 }')"
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
evidence="$(realpath -m "$1")"
mkdir -p "$evidence"
cd "$evidence"
verilator --version > compiler-version.txt
cp /usr/local/share/verilator/BOOLEY-SOURCE.txt compiler-source.txt
timeout "$((4 * guard))" verilator --binary --timing --trace-fst --top-module viewer   --Mdir obj "$source_root/tests/fixtures/verilator_acceptance/viewer.sv" > build.log 2>&1
timeout "$guard" ./obj/Vviewer > simulation.log 2>&1
test -s viewer.fst
timeout "$guard" xvfb-run -a gtkwave --version > viewer-version.txt 2>&1
timeout "$guard" xvfb-run -a gtkwave viewer.fst \
  -S "$source_root/tests/fixtures/verilator_acceptance/viewer.tcl" > viewer.log 2>&1
grep -F 'PASS: GTKWave read 2 signals' viewer.log
sha256sum viewer.fst > viewer.sha256
