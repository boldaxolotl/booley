#!/usr/bin/env bash
# Pick the stable runtime base for a CI image job (#4420): reuse the published
# REFERENCE when it carries this tree's contract, else build it locally.
# BUILD_STABLE_BASE=true skips the lookup. Writes build= and image= outputs.
set -euo pipefail
published="$1"
if [[ "${BUILD_STABLE_BASE:-false}" != "true" ]]; then
  if reason="$(python .github/scripts/docker_base_contract.py --repo . \
      --resolver remote --resolve-image "${published}" \
      --github-output "${GITHUB_OUTPUT}" 2>&1 >/dev/null)"; then
    echo "build=false" >> "${GITHUB_OUTPUT}"
    exit 0
  fi
  echo "::notice title=Stable runtime base built locally::${published} cannot serve this tree: ${reason}"
fi
echo "build=true" >> "${GITHUB_OUTPUT}"
echo "image=booley-runtime-base:ci" >> "${GITHUB_OUTPUT}"
