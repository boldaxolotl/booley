#!/usr/bin/env bash
# Run the reviewed PicoRV32 demo against the exact RISC-V candidate image.

set -euo pipefail

readonly WORKSPACE="$1"
readonly TICKET_SLUG="$2"
readonly CONTAINER_NAME="$3"

sudo chown -R 1000:1000 demo
.github/scripts/run_with_container_cleanup.sh \
  "${CONTAINER_NAME}" \
  docker run --init --rm --network none \
  --name "${CONTAINER_NAME}" \
  --mount "type=bind,src=${WORKSPACE},dst=/booley-source,readonly" \
  --mount "type=bind,src=${WORKSPACE}/demo,dst=/work" \
  --mount "type=bind,src=${WORKSPACE}/demo/.booley_project,dst=/booley-project" \
  -w /work \
  -e BOOLEY_PROJECT_DIR=/booley-project \
  -e BOOLEY_IN_SANDBOX=1 \
  -e BOOLEY_RUN_PICORV32_FLOWS=1 \
  -e PYTHONPATH=/booley-source/src \
  -e "TICKET_SLUG=${TICKET_SLUG}" \
  booley-riscv-test bash -euo pipefail -c \
  'bash /booley-source/.github/scripts/verify_picorv32_demo.sh'
