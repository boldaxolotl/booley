#!/usr/bin/env bash
# Build the sidecar candidates and retain audit evidence.

set -euo pipefail

readonly EVIDENCE_DIR="${RUNNER_TEMP}/docker-build-evidence"
readonly DOCKER_DIR="src/booley/data/docker"

# Print the FROM reference of a Dockerfile stage through the shared reader.
# SELECTOR is "only" (single-stage file), "last" (final stage), or a stage alias.
dockerfile_from() {
  PYTHONPATH=src python3 -c '
import sys
from pathlib import Path

from booley.runtime import dockerfile_syntax

references = dockerfile_syntax.from_references(Path(sys.argv[1]).read_text(encoding="utf-8"))
selector = sys.argv[2]
if selector == "only":
    chosen = references
elif selector == "last":
    chosen = references[-1:]
else:
    chosen = [reference for reference in references if reference.alias == selector]
if len(chosen) != 1 or (selector == "only" and len(references) != 1):
    sys.exit(f"{sys.argv[1]}: no unique FROM for {selector!r}")
print(chosen[0].image)
' "$@"
}

# Print "Python X.Y.Z" for a python:X.Y.Z-... base reference.
python_version_of() {
  if [[ "$1" =~ ^python:([0-9]+\.[0-9]+\.[0-9]+)- ]]; then
    printf 'Python %s\n' "${BASH_REMATCH[1]}"
  else
    echo "not a python:X.Y.Z base reference: $1" >&2
    return 1
  fi
}

# Assign first, then mark readonly, so set -e stops on a failed lookup.
BOOKWORM_CANDIDATE="$(dockerfile_from "${DOCKER_DIR}/Dockerfile.egress-proxy" only)"
readonly BOOKWORM_CANDIDATE
ALPINE_CANDIDATE="$(dockerfile_from "${DOCKER_DIR}/Dockerfile.flexnet-relay" only)"
readonly ALPINE_CANDIDATE
REAPER_BASE="$(dockerfile_from "${DOCKER_DIR}/Dockerfile.reaper" last)"
readonly REAPER_BASE
DOCKER_CLI="$(dockerfile_from "${DOCKER_DIR}/Dockerfile.reaper" docker-cli)"
readonly DOCKER_CLI
EGRESS_PYTHON="$(python_version_of "${BOOKWORM_CANDIDATE}")"
readonly EGRESS_PYTHON
FLEXNET_PYTHON="$(python_version_of "${ALPINE_CANDIDATE}")"
readonly FLEXNET_PYTHON
REAPER_PYTHON="$(python_version_of "${REAPER_BASE}")"
readonly REAPER_PYTHON
# No Dockerfile owns the daemon image; tests hold its release equal to DOCKER_CLI's.
readonly DOCKER_DIND="docker:29.9.3-dind@sha256:08a98015fce594af7c382883bea080ea206de74402a95da9dc52b6428c8cd121"

mkdir -p "${EVIDENCE_DIR}"
: > "${EVIDENCE_DIR}/build-commands.txt"
: > "${EVIDENCE_DIR}/build-times.tsv"

record_command() {
  printf '%q ' "$@" >> "${EVIDENCE_DIR}/build-commands.txt"
  printf '\n' >> "${EVIDENCE_DIR}/build-commands.txt"
}

build_image() {
  local label="$1"
  local dockerfile="$2"
  local image="$3"
  local context="$4"
  local started_at
  local finished_at
  local status
  local -a command=(
    docker build --pull --no-cache --file "${dockerfile}" --tag "${image}" "${context}"
  )

  record_command "${command[@]}"
  started_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  if "${command[@]}" 2>&1 | tee "${EVIDENCE_DIR}/${label}-build.log"; then
    status=0
  else
    status=$?
  fi
  finished_at="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  printf '%s\t%s\t%s\t%s\n' \
    "${label}" "${started_at}" "${finished_at}" "${status}" \
    >> "${EVIDENCE_DIR}/build-times.tsv"
  return "${status}"
}

capture_image() {
  local label="$1"
  local image="$2"
  local expected_version="$3"

  docker image inspect "${image}" > "${EVIDENCE_DIR}/${label}-inspect.json"
  docker history --no-trunc --format '{{json .}}' "${image}" \
    > "${EVIDENCE_DIR}/${label}-layers.jsonl"
  docker run --rm --entrypoint python3 "${image}" --version \
    > "${EVIDENCE_DIR}/${label}-python-version.txt" 2>&1
  test "$(tr -d '\r\n' < "${EVIDENCE_DIR}/${label}-python-version.txt")" = \
    "${expected_version}"
  docker run --rm --user 0:0 --entrypoint sh "${image}" -c \
    'if command -v apk >/dev/null; then apk info -vv; else dpkg-query -W; fi' \
    > "${EVIDENCE_DIR}/${label}-packages.txt"
  docker run --rm --user 0:0 --entrypoint sh "${image}" -c '
    test ! -e /usr/bin/python3
    test ! -e /root/.cache
    test ! -d /var/cache/apk || test -z "$(find /var/cache/apk -mindepth 1 -print -quit)"
    test ! -d /var/lib/apt/lists || test -z "$(find /var/lib/apt/lists -mindepth 1 -print -quit)"
  ' > "${EVIDENCE_DIR}/${label}-filesystem-audit.txt"
  printf '%s\t%s\t%s\n' \
    "${label}" "${image}" "$(docker image inspect --format '{{.Size}}' "${image}")" \
    >> "${EVIDENCE_DIR}/image-sizes.tsv"
}

capture_source() {
  local label="$1"
  local reference="$2"
  local expected_digest="${reference##*@}"
  local digests

  docker pull "${reference}" > "${EVIDENCE_DIR}/${label}-source-pull.log"
  docker image inspect "${reference}" > "${EVIDENCE_DIR}/${label}-source-inspect.json"
  digests="$(docker image inspect --format '{{join .RepoDigests " "}}' "${reference}")"
  printf '%s\t%s\t%s\n' "${label}" "${reference}" "${digests}" \
    >> "${EVIDENCE_DIR}/source-repodigests.tsv"
  grep -Fq "@${expected_digest}" <<< "${digests}"
}

build_image egress-proxy-candidate src/booley/data/docker/Dockerfile.egress-proxy \
  booley-egress-proxy:py315 src/booley/docker
build_image flexnet-relay-candidate src/booley/data/docker/Dockerfile.flexnet-relay \
  booley-flexnet-relay:py315 src/booley/eda/provisioning/licensing
build_image reaper-candidate src/booley/data/docker/Dockerfile.reaper \
  booley-reaper:py315 src/booley/docker

: > "${EVIDENCE_DIR}/image-sizes.tsv"
capture_image egress-proxy-candidate booley-egress-proxy:py315 "${EGRESS_PYTHON}"
capture_image flexnet-relay-candidate booley-flexnet-relay:py315 "${FLEXNET_PYTHON}"
capture_image reaper-candidate booley-reaper:py315 "${REAPER_PYTHON}"

: > "${EVIDENCE_DIR}/source-repodigests.tsv"
capture_source python-bookworm-candidate "${BOOKWORM_CANDIDATE}"
capture_source python-alpine-candidate "${ALPINE_CANDIDATE}"
capture_source docker-cli "${DOCKER_CLI}"
capture_source docker-dind "${DOCKER_DIND}"

docker tag booley-flexnet-relay:py315 booley-flexnet-relay:1

python .github/scripts/release_validation/evidence.py \
  --candidate-sha "${GITHUB_SHA}" \
  --image "egress-proxy=$(docker image inspect --format '{{.Id}}' booley-egress-proxy:py315)" \
  --image "flexnet-relay=$(docker image inspect --format '{{.Id}}' booley-flexnet-relay:py315)" \
  --image "reaper=$(docker image inspect --format '{{.Id}}' booley-reaper:py315)" \
  --check helper-images.build --check helper-images.runtime \
  --check helper-images.metadata --check helper-images.source-digests \
  --evidence "${EVIDENCE_DIR}/summary.json"
