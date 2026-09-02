#!/usr/bin/env bash
# Historical Python 3.13-vs-3.14 sidecar comparison; intentionally not invoked by CI.

set -euo pipefail

readonly EVIDENCE_DIR="${RUNNER_TEMP}/docker-build-evidence"
readonly CONTROL_DIR="${RUNNER_TEMP}/sidecar-controls"
readonly BOOKWORM_CONTROL="python:3.13.15-slim-bookworm@sha256:c45a22ea000adfd9cda29364bbe7edd23001ce5cc2ad15857cfbf7766943b9ca"
readonly BOOKWORM_CANDIDATE="python:3.14.7-slim-bookworm@sha256:416f0db2a2b561945630cef9877a7ea0581b27449eb9fd9df42f03e1b74b5b63"
readonly ALPINE_CONTROL="python:3.13.15-alpine3.24@sha256:540c7d91f98ff6880174c40e99067bf5941eb54d818a7a5e094d188b196a934d"
readonly ALPINE_CANDIDATE="python:3.14.7-alpine3.24@sha256:05b2b8b732ecd268fee8727a369f936f022d1321b59befd13c30ede22769dcdc"
readonly DOCKER_CLI="docker:29.7.2-cli@sha256:000bb62ff495f986c9f5578eb67cc2cb98b91138eda81d7762d5371eb8a497fe"
readonly DOCKER_DIND="docker:29.7.2-dind@sha256:3ef33f2e220b79ed3ef3b99d81746f06f306cd6340e2cb7331d17ae996e74cb6"

mkdir -p "${EVIDENCE_DIR}" "${CONTROL_DIR}"
: > "${EVIDENCE_DIR}/build-commands.txt"
: > "${EVIDENCE_DIR}/build-times.tsv"

make_control() {
  local source_file="$1"
  local output_file="$2"
  local candidate="$3"
  local control="$4"

  sed "s|${candidate}|${control}|" "${source_file}" > "${output_file}"
}

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

make_control \
  src/booley/data/docker/Dockerfile.egress-proxy \
  "${CONTROL_DIR}/Dockerfile.egress-proxy" \
  "${BOOKWORM_CANDIDATE}" "${BOOKWORM_CONTROL}"
make_control \
  src/booley/data/docker/Dockerfile.flexnet-relay \
  "${CONTROL_DIR}/Dockerfile.flexnet-relay" \
  "${ALPINE_CANDIDATE}" "${ALPINE_CONTROL}"
make_control \
  src/booley/data/docker/Dockerfile.reaper \
  "${CONTROL_DIR}/Dockerfile.reaper" \
  "${ALPINE_CANDIDATE}" "${ALPINE_CONTROL}"
cp "${CONTROL_DIR}"/Dockerfile.* "${EVIDENCE_DIR}/"

build_image egress-proxy-control "${CONTROL_DIR}/Dockerfile.egress-proxy" \
  booley-egress-proxy:py313 src/booley/docker
build_image egress-proxy-candidate src/booley/data/docker/Dockerfile.egress-proxy \
  booley-egress-proxy:py314 src/booley/docker
build_image flexnet-relay-control "${CONTROL_DIR}/Dockerfile.flexnet-relay" \
  booley-flexnet-relay:py313 src/booley/eda/provisioning/licensing
build_image flexnet-relay-candidate src/booley/data/docker/Dockerfile.flexnet-relay \
  booley-flexnet-relay:py314 src/booley/eda/provisioning/licensing
build_image reaper-control "${CONTROL_DIR}/Dockerfile.reaper" \
  booley-reaper:py313 src/booley/docker
build_image reaper-candidate src/booley/data/docker/Dockerfile.reaper \
  booley-reaper:py314 src/booley/docker

: > "${EVIDENCE_DIR}/image-sizes.tsv"
capture_image egress-proxy-control booley-egress-proxy:py313 "Python 3.13.15"
capture_image egress-proxy-candidate booley-egress-proxy:py314 "Python 3.14.7"
capture_image flexnet-relay-control booley-flexnet-relay:py313 "Python 3.13.15"
capture_image flexnet-relay-candidate booley-flexnet-relay:py314 "Python 3.14.7"
capture_image reaper-control booley-reaper:py313 "Python 3.13.15"
capture_image reaper-candidate booley-reaper:py314 "Python 3.14.7"

: > "${EVIDENCE_DIR}/source-repodigests.tsv"
capture_source python-bookworm-control "${BOOKWORM_CONTROL}"
capture_source python-bookworm-candidate "${BOOKWORM_CANDIDATE}"
capture_source python-alpine-control "${ALPINE_CONTROL}"
capture_source python-alpine-candidate "${ALPINE_CANDIDATE}"
capture_source docker-cli "${DOCKER_CLI}"
capture_source docker-dind "${DOCKER_DIND}"

docker tag booley-flexnet-relay:py314 booley-flexnet-relay:1
