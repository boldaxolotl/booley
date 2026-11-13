#!/usr/bin/env bash
# Resolve the RISC-V tooling source and build the RISC-V candidate image
# (ADR 0070). The `Tests` workflow's riscv-image job and the PicoRV32 demo
# workflow both call this file, so they build the same image the same way.
#
#   riscv_candidate.sh resolve-tooling <record>
#       Decide whether the published tooling stage or a local build supplies
#       it, and write the decision to <record> and $GITHUB_OUTPUT.
#   riscv_candidate.sh build <evidence-dir>
#       Build booley-riscv-test on booley-standard-substrate:ci. Reads
#       TOOLING_SOURCE and TOOLING_CONTEXT from the resolve-tooling outputs.

set -euo pipefail

# The published tooling stage when the main-only publisher has a
# label-verified image for this exact stage text. A missing key, an
# unreachable registry, or a slow lookup builds the stage locally instead of
# failing; a tag with the wrong labels fails. MEASUREMENT_ARM=cold skips the
# registry on purpose.
resolve_tooling() {
  local record="$1"
  local -a resolve=(
    python .github/scripts/riscv_tooling.py resolve
    --record "${record}" --github-output "${GITHUB_OUTPUT}"
  )
  if [[ "${MEASUREMENT_ARM:-automatic}" == cold ]]; then
    "${resolve[@]}" --force-local cold
    return 0
  fi
  local status=0
  timeout 120 "${resolve[@]}" || status=$?
  if (( status == 124 )); then
    echo '::warning::RISC-V tooling lookup timed out; building the stage locally.'
    "${resolve[@]}" --force-local registry-timeout
  elif (( status != 0 )); then
    return "${status}"
  fi
}

# The substrate exports as zstd level 1: BuildKit gzips single-threaded, which
# took 1-2 min for the tooling layer.
build_tool_substrate() {
  docker buildx build --builder default \
    --output type=image,name=booley-riscv-substrate:ci,compression=zstd,compression-level=1 \
    "$@" \
    --file src/booley/data/docker/Dockerfile.riscv \
    --build-context booley-standard-substrate=docker-image://booley-standard-substrate:ci .
}

build_candidate() {
  local evidence="$1"
  local tooling_record="${evidence}/tooling-source.json"
  mkdir -p "${evidence}"

  if test "${TOOLING_SOURCE}" = registry; then
    # The published digest replaces the same-named stage.
    local build_status=0
    build_tool_substrate --build-context "riscv-tooling=${TOOLING_CONTEXT}" || build_status=$?
    # Quick compatibility check of the published tooling against this
    # candidate's substrate: the contract's [riscv] probes, including the
    # shared-library closure of every host ELF.
    local compat_status=1
    if test "${build_status}" -eq 0; then
      python .github/scripts/riscv_tooling.py checks \
        > "${evidence}/tooling-checks.sh"
      compat_status=0
      docker run --rm -i --init --network none --entrypoint bash \
        booley-riscv-substrate:ci < "${evidence}/tooling-checks.sh" \
        > "${evidence}/tooling-compat.log" 2>&1 || compat_status=$?
      if test "${compat_status}" -ne 0; then
        tail -n 40 "${evidence}/tooling-compat.log" >&2
      fi
    fi
    if test "${build_status}" -ne 0 || test "${compat_status}" -ne 0; then
      local detail="composed candidate build exited ${build_status}"
      if test "${build_status}" -eq 0; then
        detail="compatibility check exited ${compat_status}"
      fi
      echo "::warning::Published RISC-V tooling ${TOOLING_CONTEXT} failed: ${detail}; building the tooling stage locally."
      python .github/scripts/riscv_tooling.py fallback \
        --record "${tooling_record}" --detail "${detail}"
      build_tool_substrate
    fi
  else
    build_tool_substrate
  fi

  docker buildx build --builder default --load \
    --file src/booley/data/docker/Dockerfile.wheel \
    --tag booley-riscv-test \
    --build-context booley-substrate=docker-image://booley-riscv-substrate:ci .
}

case "${1:-}" in
  resolve-tooling) resolve_tooling "${2:?usage: riscv_candidate.sh resolve-tooling <record>}" ;;
  build) build_candidate "${2:?usage: riscv_candidate.sh build <evidence-dir>}" ;;
  *) echo "usage: riscv_candidate.sh resolve-tooling <record> | build <evidence-dir>" >&2; exit 2 ;;
esac
