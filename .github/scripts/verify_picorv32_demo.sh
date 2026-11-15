#!/usr/bin/env bash
set -euo pipefail

export BOOLEY_MCP_MODE=interactive

booley_dir="${BOOLEY_DIR:-/booley-project}"

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python "${source_root}/.github/scripts/picorv32_demo_contract.py" \
  --contract "${source_root}/.github/contracts/picorv32-demo.toml" \
  --demo-root /work \
  --booley-dir "${booley_dir}"

python "${source_root}/.github/scripts/release_validation/measure_round_trip.py" \
  --project /work --project-state "${booley_dir}" \
  --contract "${source_root}/.github/contracts/picorv32-demo.toml" \
  --image-digest "${BOOLEY_IMAGE_DIGEST:?BOOLEY_IMAGE_DIGEST names the image under test}" \
  --evidence "${BOOLEY_MEASUREMENT_EVIDENCE:-/tmp/measurement.json}"

if [[ "${BOOLEY_RUN_PICORV32_FLOWS:-0}" == "1" ]]; then
  python -m booley.flows.lint --project /work --target lint_core
  python -m booley.flows.sim --project /work --target sim_core
fi
