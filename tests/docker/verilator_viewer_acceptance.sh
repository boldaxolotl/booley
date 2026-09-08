#!/usr/bin/env bash
# Run in an isolated environment containing the candidate compiler, GTKWave,
# Xvfb, and xauth. Retain version, source identity, FST, and reader diagnostics.
set -euo pipefail
source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
evidence="$(realpath -m "$1")"
mkdir -p "$evidence"
cd "$evidence"
verilator --version > compiler-version.txt
cp /usr/local/share/verilator/BOOLEY-SOURCE.txt compiler-source.txt
timeout 120 verilator --binary --timing --trace-fst --top-module viewer   --Mdir obj "$source_root/tests/fixtures/verilator_acceptance/viewer.sv" > build.log 2>&1
timeout 10 ./obj/Vviewer > simulation.log 2>&1
test -s viewer.fst
timeout 10 xvfb-run -a gtkwave --version > viewer-version.txt 2>&1
timeout 30 xvfb-run -a gtkwave viewer.fst \
  -S "$source_root/tests/fixtures/verilator_acceptance/viewer.tcl" > viewer.log 2>&1
grep -F 'PASS: GTKWave read 2 signals' viewer.log
sha256sum viewer.fst > viewer.sha256
