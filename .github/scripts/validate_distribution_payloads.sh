#!/usr/bin/env bash
set -euo pipefail

dist_dir="${1:-dist}"
shopt -s nullglob
sdists=("${dist_dir}"/*.tar.gz)
wheels=("${dist_dir}"/*.whl)

if (( ${#sdists[@]} != 1 )); then
    echo "expected exactly one source distribution in ${dist_dir}, found ${#sdists[@]}" >&2
    exit 1
fi
if (( ${#wheels[@]} != 1 )); then
    echo "expected exactly one wheel in ${dist_dir}, found ${#wheels[@]}" >&2
    exit 1
fi

sdist="${sdists[0]}"
wheel="${wheels[0]}"

if ! tar -tzf "${sdist}" | grep -E '/src/booley/data/refs/CHANGELOG\.md$' >/dev/null; then
    echo "packaged changelog missing from source distribution" >&2
    exit 1
fi
if ! unzip -Z1 "${wheel}" | grep -Fx 'booley/data/refs/CHANGELOG.md' >/dev/null; then
    echo "packaged changelog missing from wheel" >&2
    exit 1
fi

if tar -tzf "${sdist}" | grep -E '/src/booley/data/docker/pdk/'; then
    echo "Nangate payload found in source distribution" >&2
    exit 1
fi
if unzip -Z1 "${wheel}" | grep -E '^booley/data/docker/pdk/'; then
    echo "Nangate payload found in wheel" >&2
    exit 1
fi
if unzip -Z1 "${wheel}" | grep -E '^booley/data/bin/bwave(\.exe)?$'; then
    echo "Native bwave payload found in platform-neutral wheel" >&2
    exit 1
fi
unzip -p "${wheel}" '*/METADATA' | grep -Fx 'License-Expression: Apache-2.0'
