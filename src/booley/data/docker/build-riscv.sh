#!/bin/bash
# Build the wheel-free RISC-V substrate, then apply the current wheel overlay.
# Usage: ./build-riscv.sh [--no-cache]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOLEY_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Build the runtime base, standard substrate, and exact wheel once.
echo ">>> Preparing standard substrate and wheel..."
"$SCRIPT_DIR/build.sh" "$@"

FP_PY=""
for cand in "${PYTHON:-}" python3 /usr/bin/python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 \
     && PYTHONPATH="$BOOLEY_ROOT/src" "$cand" -c 'import booley.harness.init_cmd' 2>/dev/null; then
    FP_PY="$cand"; break
  fi
done
FP_PY="${FP_PY:-python3}"
WHEEL_SOURCE_FINGERPRINT="$(PYTHONPATH="$BOOLEY_ROOT/src" "$FP_PY" -P -c \
  'import sys; from pathlib import Path; from booley.runtime.build_stamp import resolve_wheel_source_fingerprint; print(resolve_wheel_source_fingerprint(Path(sys.argv[1])) or "")' \
  "$BOOLEY_ROOT")"
WHEEL_SHA256="$(sha256sum "$BOOLEY_ROOT"/dist/booley_rtl-*.whl | cut -d' ' -f1)"
RECIPE_FINGERPRINT="$(PYTHONPATH="$BOOLEY_ROOT/src" "$FP_PY" -P -c \
  'import sys; from pathlib import Path; from booley.runtime.image_provenance import resolve_recipe_fingerprint; print(resolve_recipe_fingerprint((Path(sys.argv[1]),)))' \
  "$SCRIPT_DIR/Dockerfile.riscv")"
STANDARD_ID="$(docker image inspect booley-sandbox-standard-substrate:local --format '{{.Id}}')"

run_docker_build() {
  local image="$1"
  shift
  PYTHONPATH="$BOOLEY_ROOT/src" "${FP_PY:-python3}" -P -m booley.runtime.docker_capacity \
    --image "$image" -- "$@"
  "$@"
}

echo ">>> Building RISC-V tool substrate..."
run_docker_build booley-sandbox-riscv-substrate:local docker build "$@" \
  --label "io.booley.provenance.schema=3" \
  --label "io.booley.artifact.role=riscv-substrate" \
  --label "io.booley.artifact.effective-inputs=$RECIPE_FINGERPRINT" \
  --label "io.booley.build.recipe-fingerprint=$RECIPE_FINGERPRINT" \
  --label "io.booley.build.parent-artifact-kind=local-image-id" \
  --label "io.booley.build.parent-artifact=$STANDARD_ID" \
  --label "io.booley.build.origin=local" \
  --build-context booley-standard-substrate=docker-image://booley-sandbox-standard-substrate:local \
  -t booley-sandbox-riscv-substrate:local \
  -f "$SCRIPT_DIR/Dockerfile.riscv" "$SCRIPT_DIR"
RISCV_ID="$(docker image inspect booley-sandbox-riscv-substrate:local --format '{{.Id}}')"
OVERLAY_RECIPE="$(PYTHONPATH="$BOOLEY_ROOT/src" "$FP_PY" -P -c \
  'import sys; from pathlib import Path; from booley.runtime.image_provenance import resolve_recipe_fingerprint; print(resolve_recipe_fingerprint((Path(sys.argv[1]),)))' \
  "$SCRIPT_DIR/Dockerfile.wheel")"

echo ">>> Building booley-sandbox-riscv wheel overlay..."
run_docker_build booley-sandbox-riscv docker build "$@" \
  --build-arg "BOOLEY_VERSION=$(cat "$BOOLEY_ROOT/VERSION")" \
  --build-arg "BOOLEY_SOURCE_REVISION=$(git -C "$BOOLEY_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)" \
  --build-arg "BOOLEY_SOURCE_UPDATED_AT=$(git -C "$BOOLEY_ROOT" log -1 --format=%cI HEAD 2>/dev/null || echo unknown)" \
  --build-arg "BOOLEY_IMAGE_BUILT_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --build-arg "BOOLEY_PAYLOAD_FINGERPRINT=$WHEEL_SOURCE_FINGERPRINT" \
  --build-arg "BOOLEY_WHEEL_SHA256=$WHEEL_SHA256" \
  --label "io.booley.provenance.schema=3" \
  --label "io.booley.artifact.role=wheel-overlay" \
  --label "io.booley.artifact.effective-inputs=$WHEEL_SOURCE_FINGERPRINT" \
  --label "io.booley.wheel.source-fingerprint=$WHEEL_SOURCE_FINGERPRINT" \
  --label "io.booley.wheel.sha256=$WHEEL_SHA256" \
  --label "io.booley.build.recipe-fingerprint=$OVERLAY_RECIPE" \
  --label "io.booley.build.parent-artifact-kind=local-image-id" \
  --label "io.booley.build.parent-artifact=$RISCV_ID" \
  --label "io.booley.build.origin=local" \
  --build-context booley-substrate=docker-image://booley-sandbox-riscv-substrate:local \
  -t booley-sandbox-riscv -f "$SCRIPT_DIR/Dockerfile.wheel" "$BOOLEY_ROOT"
echo "✓ booley-sandbox-riscv image built successfully"
