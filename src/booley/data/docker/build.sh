#!/bin/bash
# Build the booley-sandbox Docker image.
# Builds the Python wheel first, then runs docker build.
# Usage: ./build.sh [--no-cache]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BOOLEY_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

# Resolve a Python that actually ships the pypa `build` module. A project
# `.venv` is often first on PATH but installs Booley without its build deps, so
# a bare `python3` silently fails the wheel step below — and with `set -e` the
# abort is easy to miss when the real damage is the docker COPY layer reusing a
# stale wheel from a previous run. Prefer $PYTHON, then PATH python3, then the
# system interpreter. Fail loud if none can build.
#
# The probe MUST use `-P` (as the wheel step below does): without it, `-c` puts
# the cwd on sys.path, and the repo-root `build/` setuptools artifact dir (a bare
# namespace package) satisfies `import build` — so the probe green-lights a Python
# that has no *pypa* build, then `-P -m build` fails with "No module named build".
# That is the exact false pass this guard exists to prevent, so it has to test
# the same interpreter state the real invocation runs under.
PYBUILD=""
for cand in "${PYTHON:-}" python3 /usr/bin/python3 python; do
  [ -n "$cand" ] || continue
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -P -c 'import build' 2>/dev/null; then
    PYBUILD="$cand"; break
  fi
done
if [ -z "$PYBUILD" ]; then
  echo "ERROR: no Python with the pypa 'build' module found." >&2
  echo "       Install it (e.g. 'python3 -m pip install build') or set \$PYTHON" >&2
  echo "       to an interpreter that has it — the .venv usually does not." >&2
  exit 1
fi
echo ">>> Using Python for build: $("$PYBUILD" -c 'import sys; print(sys.executable)')"

# Build wheel into dist/ (docker build COPY expects it there).
# -P: keep the repo-root build/ setuptools artifact dir from shadowing the
# pypa `build` module (python -m prepends cwd to sys.path otherwise; the
# failure mode is the docker COPY layer silently reusing a stale wheel).
# Stamp the source commit into the package so the wheel can report which
# commit it was built from. A wheel install has no adjacent .git, so without
# this every image reports a bare `booley <version>` and the prescribed
# freshness check ("confirm the wheel matches the commit") cannot be answered
# (F-5). Generated, not tracked — see .gitignore.
#
# The rule for *what* to stamp (short HEAD, `+dirty` suffix, empty outside a
# checkout) lives in booley.runtime.build_stamp, which `booley init`'s own wheel
# build calls too — a second copy here is how init ended up building unstamped
# wheels in the first place (F-3).
STAMP="$BOOLEY_ROOT/src/booley/_build_commit.py"
# Transient: the stamp only has to survive until the wheel is built and the
# docker COPY layer runs. Leaving it behind makes the checkout report a
# baked commit it doesn't have (and fails test_absent_stamp_module_yields_none).
trap 'rm -f "$STAMP"' EXIT
COMMIT="$(PYTHONPATH="$BOOLEY_ROOT/src" "$PYBUILD" -P -c \
  'import sys; from pathlib import Path; from booley.runtime.build_stamp import BuildProfile, write_build_stamp; print(write_build_stamp(Path(sys.argv[1]), profile=BuildProfile.RUNTIME_IMAGE))' \
  "$BOOLEY_ROOT")"
echo ">>> Stamped build commit: ${COMMIT:-<unknown>}"

echo ">>> Building booley wheel..."
# setuptools incrementally reuses build/lib. That is unsafe across package
# moves: deleted modules remain in the staging tree and leak into the next
# wheel.
# build/ is generated packaging state, so always recreate it from the source
# tree before producing the wheel consumed by Docker.
rm -rf "$BOOLEY_ROOT/build"
# Docker COPY and pip expand this glob, so an older version must not survive
# beside the wheel produced by this run (GitHub issue #66).
rm -f "$BOOLEY_ROOT"/dist/booley_rtl-*.whl
# Marker predating the build: the freshness check below proves the wheel the
# docker COPY layer will pick up was actually (re)written by *this* run, not left
# over from a previous one. Belt-and-suspenders behind `set -e`: if the wheel
# step ever exits 0 without producing a wheel, we still fail loud here instead of
# baking a stale wheel into the image (the very failure the guard above prevents).
WHEEL_MARKER="$(mktemp)"
trap 'rm -f "$STAMP" "$WHEEL_MARKER"' EXIT
(cd "$BOOLEY_ROOT" && "$PYBUILD" -P -m build --wheel --outdir dist/)

# Exactly one wheel in dist/ must be newer than the marker, or Docker's glob
# would either fail to COPY a wheel or pass conflicting versions to pip.
mapfile -t FRESH_WHEELS < <(find "$BOOLEY_ROOT/dist" -maxdepth 1 -type f -name 'booley_rtl-*.whl' -newer "$WHEEL_MARKER" -print 2>/dev/null)
if [ "${#FRESH_WHEELS[@]}" -ne 1 ]; then
  echo "ERROR: the wheel step produced ${#FRESH_WHEELS[@]} fresh dist/booley_rtl-*.whl files; expected exactly one." >&2
  echo "       The docker COPY requires one unambiguous wheel — aborting." >&2
  exit 1
fi
FRESH_WHEEL="${FRESH_WHEELS[0]}"
echo ">>> Fresh wheel: $(basename "$FRESH_WHEEL")"

WHEEL_SOURCE_FINGERPRINT="$(PYTHONPATH="$BOOLEY_ROOT/src" "$PYBUILD" -P -c \
  'import sys; from pathlib import Path; from booley.runtime.build_stamp import resolve_wheel_source_fingerprint; print(resolve_wheel_source_fingerprint(Path(sys.argv[1])) or "")' \
  "$BOOLEY_ROOT")"
WHEEL_SHA256="$(sha256sum "$FRESH_WHEEL" | cut -d' ' -f1)"
STANDARD_INPUTS="$(PYTHONPATH="$BOOLEY_ROOT/src" "$PYBUILD" -P -c \
  'import sys; from pathlib import Path; from booley.runtime.image_lifecycle import standard_substrate_fingerprint; print(standard_substrate_fingerprint(Path(sys.argv[1])))' \
  "$BOOLEY_ROOT")"
recipe_fingerprint() {
  PYTHONPATH="$BOOLEY_ROOT/src" "$PYBUILD" -P -c \
    'import sys; from pathlib import Path; from booley.runtime.image_provenance import resolve_recipe_fingerprint; print(resolve_recipe_fingerprint((Path(sys.argv[1]),)))' "$1"
}
SOURCE_UPDATED_AT="$(git -C "$BOOLEY_ROOT" log -1 --format=%cI HEAD 2>/dev/null || true)"
IMAGE_BUILT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
BUILD_METADATA_ARGS=(
  --build-arg "BOOLEY_VERSION=$(cat "$BOOLEY_ROOT/VERSION")"
  --build-arg "BOOLEY_SOURCE_REVISION=${COMMIT:-unknown}"
  --build-arg "BOOLEY_SOURCE_UPDATED_AT=${SOURCE_UPDATED_AT:-unknown}"
  --build-arg "BOOLEY_IMAGE_BUILT_AT=$IMAGE_BUILT_AT"
  --build-arg "BOOLEY_PAYLOAD_FINGERPRINT=${WHEEL_SOURCE_FINGERPRINT:-unknown}"
)

BASE_CONTRACT="$($PYBUILD "$BOOLEY_ROOT/.github/scripts/docker_base_contract.py" \
  --repo "$BOOLEY_ROOT")"
BASE_METADATA_ARGS=(
  --build-arg "BOOLEY_BASE_SOURCE_REVISION=${COMMIT:-unknown}"
  --build-arg "BOOLEY_BASE_CONTRACT=$BASE_CONTRACT"
  --build-arg "BOOLEY_BASE_BUILT_AT=$IMAGE_BUILT_AT"
)

run_docker_build() {
  local image="$1"
  shift
  PYTHONPATH="$BOOLEY_ROOT/src" "$PYBUILD" -P -m booley.runtime.docker_capacity \
    --image "$image" -- "$@"
  "$@"
}

echo ">>> Building stable EDA/runtime base (cacheable across candidate changes)..."
run_docker_build booley-runtime-base:local docker build "${BASE_METADATA_ARGS[@]}" "$@" \
  --label "io.booley.provenance.schema=3" \
  --label "io.booley.artifact.role=runtime-base" \
  --label "io.booley.artifact.effective-inputs=$BASE_CONTRACT" \
  --label "io.booley.build.recipe-fingerprint=$(recipe_fingerprint "$SCRIPT_DIR/Dockerfile.base")" \
  --label "io.booley.build.origin=local" \
  -t booley-runtime-base:local -f "$SCRIPT_DIR/Dockerfile.base" "$BOOLEY_ROOT"
RUNTIME_BASE_ID="$(docker image inspect booley-runtime-base:local --format '{{.Id}}')"

echo ">>> Building standard tool substrate..."
run_docker_build booley-sandbox-standard-substrate:local docker build "$@" \
  --label "io.booley.provenance.schema=3" \
  --label "io.booley.artifact.role=standard-substrate" \
  --label "io.booley.artifact.effective-inputs=$STANDARD_INPUTS" \
  --label "io.booley.build.recipe-fingerprint=$(recipe_fingerprint "$SCRIPT_DIR/Dockerfile.substrate")" \
  --label "io.booley.build.parent-artifact-kind=local-image-id" \
  --label "io.booley.build.parent-artifact=$RUNTIME_BASE_ID" \
  --label "io.booley.build.origin=local" \
  --build-context booley-runtime-base=docker-image://booley-runtime-base:local \
  -t booley-sandbox-standard-substrate:local \
  -f "$SCRIPT_DIR/Dockerfile.substrate" "$BOOLEY_ROOT"
STANDARD_ID="$(docker image inspect booley-sandbox-standard-substrate:local --format '{{.Id}}')"

echo ">>> Building booley-sandbox wheel overlay..."
run_docker_build booley-sandbox docker build "${BUILD_METADATA_ARGS[@]}" "$@" \
  --label "io.booley.provenance.schema=3" \
  --label "io.booley.artifact.role=wheel-overlay" \
  --label "io.booley.artifact.effective-inputs=$WHEEL_SOURCE_FINGERPRINT" \
  --label "io.booley.wheel.source-fingerprint=$WHEEL_SOURCE_FINGERPRINT" \
  --label "io.booley.wheel.sha256=$WHEEL_SHA256" \
  --label "io.booley.build.recipe-fingerprint=$(recipe_fingerprint "$SCRIPT_DIR/Dockerfile.wheel")" \
  --label "io.booley.build.parent-artifact-kind=local-image-id" \
  --label "io.booley.build.parent-artifact=$STANDARD_ID" \
  --label "io.booley.build.origin=local" \
  --build-context booley-substrate=docker-image://booley-sandbox-standard-substrate:local \
  -t booley-sandbox -f "$SCRIPT_DIR/Dockerfile.wheel" "$BOOLEY_ROOT"
echo "✓ booley-sandbox image built successfully"
