#!/usr/bin/env bash
# Validate the exact RISC-V candidate image and retain contract evidence.

set -euo pipefail

readonly EVIDENCE_DIR="$1"
readonly IMAGE="$2"
readonly BASE_IMAGE="$3"

PYTHONPATH=src python .github/scripts/image_contract.py \
  --image "${IMAGE}" \
  --base-image "${BASE_IMAGE}" \
  --flavor riscv \
  --contract .github/contracts/session-runtime.toml \
  --evidence "${EVIDENCE_DIR}/runtime-contract.json"
PYTHONPATH=src python .github/scripts/image_size_report.py \
  --runtime-image sandbox=booley-test \
  --runtime-image "riscv=${IMAGE}" \
  --limits .github/contracts/image-size-limits.toml \
  --json "${EVIDENCE_DIR}/image-sizes.json" \
  --markdown "${EVIDENCE_DIR}/image-sizes.md"
python .github/scripts/image_runtime_resources.py \
  --image "riscv=${IMAGE}" \
  --json "${EVIDENCE_DIR}/runtime-resources.json" \
  --markdown "${EVIDENCE_DIR}/runtime-resources.md"
cat "${EVIDENCE_DIR}/image-sizes.md" >> "${GITHUB_STEP_SUMMARY}"
cat "${EVIDENCE_DIR}/runtime-resources.md" >> "${GITHUB_STEP_SUMMARY}"
