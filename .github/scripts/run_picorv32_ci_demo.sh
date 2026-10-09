#!/usr/bin/env bash
# Run the reviewed PicoRV32 demo against the exact RISC-V candidate image.

set -euo pipefail

readonly WORKSPACE="$1"
readonly CONTAINER_NAME="$2"
readonly EVIDENCE_DIR="${RUNNER_TEMP}/riscv-image-evidence/goal-readiness"

mkdir -p "${EVIDENCE_DIR}"
sudo chown -R 1000:1000 demo "${EVIDENCE_DIR}"
.github/scripts/run_with_container_cleanup.sh \
  "${CONTAINER_NAME}" \
  docker run --init --rm --network none \
  --name "${CONTAINER_NAME}" \
  --mount "type=bind,src=${WORKSPACE},dst=/booley-source,readonly" \
  --mount "type=bind,src=${WORKSPACE}/demo,dst=/work" \
  --mount "type=bind,src=${WORKSPACE}/demo/.booley_project,dst=/booley-project" \
  --mount "type=bind,src=${EVIDENCE_DIR},dst=/evidence" \
  -w /work \
  -e BOOLEY_PROJECT_DIR=/booley-project \
  -e BOOLEY_GOAL_READINESS_EVIDENCE=/evidence/goal-readiness.json \
  -e BOOLEY_IN_SANDBOX=1 \
  -e BOOLEY_RUN_PICORV32_FLOWS=1 \
  -e PYTHONPATH=/booley-source/src \
  -e BOOLEY_MCP_MODE=interactive \
  booley-riscv-test bash -euo pipefail -c \
  'bash /booley-source/.github/scripts/verify_picorv32_demo.sh'
