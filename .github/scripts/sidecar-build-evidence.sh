#!/usr/bin/env bash
# Build the sidecar candidates and retain audit evidence.

set -euo pipefail

readonly EVIDENCE_DIR="${RUNNER_TEMP}/docker-build-evidence"
readonly BOOKWORM_CANDIDATE="python:3.14.7-slim-bookworm@sha256:9ab8d9c8514b44f90cf0029dd42fdd7e9e211e639c8b995304cc04568dee900f"
readonly ALPINE_CANDIDATE="python:3.14.7-alpine3.24@sha256:c6ead215bfd31f1e433d968853b7a769989117115b728874824e6c0a27cb96fc"
readonly DOCKER_CLI="docker:29.7.2-cli@sha256:3f4743208d2338c934d7b8bcfbe1bb54c0b2355c510ad5e0f31c0c4a54bd704e"
readonly DOCKER_DIND="docker:29.7.2-dind@sha256:3ef33f2e220b79ed3ef3b99d81746f06f306cd6340e2cb7331d17ae996e74cb6"

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
  booley-egress-proxy:py314 src/booley/docker
build_image flexnet-relay-candidate src/booley/data/docker/Dockerfile.flexnet-relay \
  booley-flexnet-relay:py314 src/booley/eda/provisioning/licensing
build_image reaper-candidate src/booley/data/docker/Dockerfile.reaper \
  booley-reaper:py314 src/booley/docker

: > "${EVIDENCE_DIR}/image-sizes.tsv"
capture_image egress-proxy-candidate booley-egress-proxy:py314 "Python 3.14.7"
capture_image flexnet-relay-candidate booley-flexnet-relay:py314 "Python 3.14.7"
capture_image reaper-candidate booley-reaper:py314 "Python 3.14.7"

: > "${EVIDENCE_DIR}/source-repodigests.tsv"
capture_source python-bookworm-candidate "${BOOKWORM_CANDIDATE}"
capture_source python-alpine-candidate "${ALPINE_CANDIDATE}"
capture_source docker-cli "${DOCKER_CLI}"
capture_source docker-dind "${DOCKER_DIND}"

docker tag booley-flexnet-relay:py314 booley-flexnet-relay:1
