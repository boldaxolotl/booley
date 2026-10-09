#!/usr/bin/env bash
set -euo pipefail

export BOOLEY_MCP_MODE=interactive

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python "${source_root}/.github/scripts/picorv32_demo_contract.py" \
  --contract "${source_root}/.github/contracts/picorv32-demo.toml" \
  --demo-root /work \
  --project-dir /booley-project

python "${source_root}/.github/scripts/goal_mode_driver.py" \
  --project /work --project-state /booley-project \
  --contract "${source_root}/.github/contracts/picorv32-demo.toml" \
  --readiness --evidence "${BOOLEY_GOAL_READINESS_EVIDENCE:-/tmp/goal-readiness.json}"

if [[ "${BOOLEY_RUN_PICORV32_FLOWS:-0}" == "1" ]]; then
  python -m booley.flows.lint --project /work --target lint_core
  python -m booley.flows.sim --project /work --target sim_core
fi
