#!/usr/bin/env bash
set -euo pipefail

: "${TICKET_SLUG:?TICKET_SLUG is required}"

source_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
python -m booley.harness.booley run --ticket "${TICKET_SLUG}" --check-ready
python "${source_root}/.github/scripts/picorv32_demo_contract.py" \
  --contract "${source_root}/.github/contracts/picorv32-demo.toml" \
  --demo-root /work \
  --project-dir /booley-project
