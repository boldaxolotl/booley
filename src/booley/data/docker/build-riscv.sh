#!/bin/bash
# Build the booley-sandbox-riscv Docker image (base sandbox + RISC-V toolchain,
# Spike ISS, and the ratified RISC-V spec set — see Dockerfile.riscv).
#
# It layers on booley-sandbox, so this first (re)builds the base via build.sh —
# guaranteeing the RISC-V image never freezes stale base layers (the
# derived-image drift trap). Pass --no-cache to force a clean rebuild.
# Usage: ./build-riscv.sh [--no-cache]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOLEY_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Build the base image (and the wheel it bakes) first.
echo ">>> Building base booley-sandbox image first..."
"$SCRIPT_DIR/build.sh" "$@"

# Stamp the same build-fingerprint label `booley init` / build.sh use, so the
# derived RISC-V image is treated as fresh against the current base sources and
# doctor's freshness guard doesn't flag it. Empty on failure -> no label.
# Same interpreter caveat as build.sh: a .venv python3 may import booley but is
# not guaranteed to be the one build.sh used. Prefer $PYTHON, then the system
# interpreter, so the fingerprint here matches the one stamped on the base.
FP_PY=""
for cand in "${PYTHON:-}" python3 /usr/bin/python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 \
     && PYTHONPATH="$BOOLEY_ROOT/src" "$cand" -c 'import booley.harness.init_cmd' 2>/dev/null; then
    FP_PY="$cand"; break
  fi
done
FINGERPRINT="$(PYTHONPATH="$BOOLEY_ROOT/src" "${FP_PY:-python3}" -c \
  'import sys; from pathlib import Path; from booley.harness.init_cmd import _image_build_fingerprint; print(_image_build_fingerprint(Path(sys.argv[1])) or "")' \
  "$BOOLEY_ROOT" 2>/dev/null || true)"
LABEL_ARGS=()
[ -n "$FINGERPRINT" ] && LABEL_ARGS=(--label "booley.build-fingerprint=$FINGERPRINT")
BASE_IMAGE_ID="$(docker image inspect booley-sandbox --format '{{.Id}}')"
[ -n "$BASE_IMAGE_ID" ] && LABEL_ARGS+=(--label "booley.base-image-id=$BASE_IMAGE_ID")
[ -n "$FINGERPRINT" ] && LABEL_ARGS+=(
  --label "io.booley.provenance.schema=1"
  --label "io.booley.payload.fingerprint=$FINGERPRINT"
  --label "io.booley.build.origin=local"
)
[ -n "$BASE_IMAGE_ID" ] && LABEL_ARGS+=(--label "io.booley.build.parent-artifact=$BASE_IMAGE_ID")
RECIPE_FINGERPRINT="$(PYTHONPATH="$BOOLEY_ROOT/src" "${FP_PY:-python3}" -c \
  'import sys; from pathlib import Path; from booley.runtime.image_provenance import resolve_recipe_fingerprint; print(resolve_recipe_fingerprint((Path(sys.argv[1]),)))' \
  "$SCRIPT_DIR/Dockerfile.riscv")"
LABEL_ARGS+=(--label "io.booley.build.recipe-fingerprint=$RECIPE_FINGERPRINT")

echo ">>> Building booley-sandbox-riscv Docker image..."
docker build "${LABEL_ARGS[@]}" "$@" -t booley-sandbox-riscv \
    -f "$SCRIPT_DIR/Dockerfile.riscv" "$BOOLEY_ROOT"
echo "✓ booley-sandbox-riscv image built successfully"
