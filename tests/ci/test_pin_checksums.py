"""Contracts for ``.github/scripts/pin_checksums.py`` (#2560).

Nothing here touches the network. ``fixtures/pin_checksums/recorded.json``
holds upstream metadata recorded on 2026-11-08 at the pins' versions (tags,
commits, npm version and repositories are the real values). The artifacts
themselves are up to hundreds of megabytes, so ``_stand_in`` serves small
deterministic bodies and the recorded digest fields (GitHub ``digest``, Open
VSX ``.sha256``, Node ``SHASUMS256.txt``) carry the stand-ins' sha256. The
xPack stand-in is larger than one hashing chunk. ``fixtures/pin_checksums/repo``
is a frozen excerpt of the four pinned files holding the expected values, so a
future Renovate bump cannot break these tests.
"""

from __future__ import annotations

import functools
import importlib
import io
import json
import os
import re
import shutil
import sys
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / ".github/scripts"))

_FIXTURES = Path(__file__).parent / "fixtures/pin_checksums"
_RECORDED: dict[str, Any] = json.loads((_FIXTURES / "recorded.json").read_text(encoding="utf-8"))

_BASE = "src/booley/data/docker/Dockerfile.base"
_RISCV = "src/booley/data/docker/Dockerfile.riscv"
_WORKFLOW = ".github/workflows/test.yml"
_DEMO = ".github/contracts/picorv32-demo.toml"
_GITHUB = frozenset({"github.com", "api.github.com"})
_OPEN_VSX = frozenset({"open-vsx.org", "api.github.com"})
_NODE = frozenset({"nodejs.org"})
_LARGE_STAND_IN = "riscv-none-elf-gcc-xpack"
_ARTIFACT = re.compile(r"/releases/download/|\.vsix$|\.tar\.xz$")


def _arg(*names: str) -> list[str]:
    """Return line patterns, each with one group, for ``ARG`` defaults."""
    return [rf"^ARG {name}=(.*)$" for name in names]


def _vsix(prefix: str) -> list[str]:
    return _arg(f"{prefix}_SHA256", f"{prefix}_SOURCE_TAG", f"{prefix}_SOURCE_COMMIT")


# id -> (depName, version, hosts it may fetch from, {file: patterns of its rewritten values})
_CASES: dict[str, tuple[str, str, frozenset[str], dict[str, list[str]]]] = {
    "sv2v": ("zachjs/sv2v", "v0.0.13", _GITHUB, {_BASE: _arg("SV2V_SHA256")}),
    "verible": (
        "chipsalliance/verible",
        "v0.0-4296-g0f262651",
        _GITHUB,
        {_BASE: _arg("VERIBLE_SHA256")},
    ),
    "xpack-gcc": (
        "xpack-dev-tools/riscv-none-elf-gcc-xpack",
        "15.2.0-1",
        _GITHUB,
        {
            _RISCV: _arg("XPACK_GCC_SHA256"),
            _DEMO: [r'^toolchain_url = "(.*)"$', r'^toolchain_sha256 = "(.*)"$'],
        },
    ),
    "isa-manual": (
        "riscv/riscv-isa-manual",
        "20250508",
        _GITHUB,
        {
            _RISCV: _arg(
                "ISA_UNPRIVILEGED_PDF_SHA256",
                "ISA_UNPRIVILEGED_HTML_SHA256",
                "ISA_PRIVILEGED_PDF_SHA256",
                "ISA_PRIVILEGED_HTML_SHA256",
            )
        },
    ),
    "debug-spec": ("riscv/riscv-debug-spec", "1.0", _GITHUB, {_RISCV: _arg("DEBUG_SPEC_SHA256")}),
    "psabi": (
        "riscv-non-isa/riscv-elf-psabi-doc",
        "v1.0",
        _GITHUB,
        {_RISCV: _arg("PSABI_SHA256")},
    ),
    "actionlint": (
        "rhysd/actionlint",
        "v1.7.12",
        _GITHUB,
        {_WORKFLOW: [r"^ +ACTIONLINT_SHA256: (.*)$"]},
    ),
    "vaporview": ("openvsx:lramseyer/vaporview", "1.6.0", _OPEN_VSX, {_BASE: _vsix("VAPORVIEW")}),
    "veriloghdl": ("openvsx:mshr-h/veriloghdl", "1.29.0", _OPEN_VSX, {_BASE: _vsix("VERILOGHDL")}),
    "live-preview": (
        "openvsx:ms-vscode/live-server",
        "0.4.20",
        _OPEN_VSX,
        {_BASE: _vsix("LIVE_PREVIEW")},
    ),
    "node": ("node", "24.21.0", _NODE, {_BASE: _arg("NODE_SHA256", "NPM_VERSION")}),
}


@functools.cache
def _script() -> Any:
    """Load the script on first use so a missing script fails the test body."""
    return importlib.import_module("pin_checksums")


def _stand_in(url: str) -> bytes:
    body = f"stand-in for {url}\n".encode()
    return body + b"\0" * (1_048_583 if _LARGE_STAND_IN in url else 0)


class _Upstream:
    """In-memory ``opener``: recorded metadata and stand-in artifacts, with a request log."""

    def __init__(self, overrides: dict[str, bytes | None] | None = None) -> None:
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._overrides = overrides or {}

    def __call__(self, request: Any) -> io.BytesIO:
        url = request.full_url
        self.requests.append((url, dict(request.header_items())))
        if url in self._overrides:
            body = self._overrides[url]
        elif url in _RECORDED:
            recorded = _RECORDED[url]
            body = (recorded if isinstance(recorded, str) else json.dumps(recorded)).encode()
        elif _ARTIFACT.search(url):
            body = _stand_in(url)
        else:
            body = None
        if body is None:
            raise urllib.error.URLError(f"unreachable: {url}")
        return io.BytesIO(body)

    def hosts(self) -> set[str | None]:
        return {urllib.parse.urlsplit(url).hostname for url, _ in self.requests}


def _snapshot(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _stale(match: re.Match[str]) -> str:
    start = match.start(0)
    return (
        match.group(0)[: match.start(1) - start] + "stale" + match.group(0)[match.end(1) - start :]
    )


def _checkout(tmp_path: Path, cases: list[str]) -> Path:
    """Copy the frozen tree and stale every value the given cases rewrite."""
    root = tmp_path / "repo"
    shutil.copytree(_FIXTURES / "repo", root)
    for case in cases:
        for file, patterns in _CASES[case][3].items():
            text = (root / file).read_bytes().decode()
            for pattern in patterns:
                text, count = re.subn(pattern, _stale, text, flags=re.MULTILINE)
                assert count == 1, pattern
            (root / file).write_bytes(text.encode())
    return root


def _data_file(tmp_path: Path, upgrades: list[tuple[str, str]]) -> Path:
    path = tmp_path / "upgrades.json"
    body = {"upgrades": [{"depName": d, "currentValue": "0", "newValue": v} for d, v in upgrades]}
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def _run(root: Path, data_file: Path, upstream: _Upstream) -> int:
    return _script().main([str(data_file), "--root", str(root)], opener=upstream)


@pytest.fixture(autouse=True)
def _no_ambient_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


@pytest.mark.parametrize("case", list(_CASES))
def test_known_pin_rewrites_paired_values_from_recorded_upstream(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dep_name, version, hosts, patterns = _CASES[case]
    monkeypatch.setenv("GITHUB_TOKEN", "token-for-api")
    root = _checkout(tmp_path, [case])
    before = _snapshot(root)
    upstream = _Upstream()

    assert _run(root, _data_file(tmp_path, [(dep_name, version)]), upstream) == 0

    expected = _snapshot(_FIXTURES / "repo")
    after = _snapshot(root)
    assert after == expected
    assert {name for name in after if after[name] != before[name]} == set(patterns)
    assert upstream.hosts() == hosts
    for url, headers in upstream.requests:
        on_api = urllib.parse.urlsplit(url).hostname == "api.github.com"
        assert ("Authorization" in headers) == on_api, url


def test_real_files_hold_every_pin_location_once() -> None:
    script = _script()
    tree = script.Tree(_ROOT)
    assert set(script.PINS) == {dep for dep, *_ in _CASES.values()}
    for pin in script.PINS.values():
        for value in pin.values:
            tree.read(value)
        for download in pin.downloads:
            assert tree.url(download, pin.version.file).startswith("https://")


def test_second_run_changes_nothing(tmp_path: Path) -> None:
    script = _script()
    root = _checkout(tmp_path, list(_CASES))
    upgrades = [(dep, version) for dep, version, *_ in _CASES.values()]
    upstream = script.Upstream(_Upstream())

    first = script.rewrite(root, upgrades, upstream)
    assert first
    assert _snapshot(root) == _snapshot(_FIXTURES / "repo")
    assert script.rewrite(root, upgrades, upstream) == []
    assert _snapshot(root) == _snapshot(_FIXTURES / "repo")


_SV2V_URL = "https://github.com/zachjs/sv2v/releases/download/v0.0.13/sv2v-Linux.zip"
_VERIBLE_URL = (
    "https://github.com/chipsalliance/verible/releases/download/v0.0-4296-g0f262651/"
    "verible-v0.0-4296-g0f262651-linux-static-x86_64.tar.gz"
)
_NODE_INDEX_URL = "https://nodejs.org/dist/index.json"
_VAPORVIEW_SHA_URL = (
    "https://open-vsx.org/api/lramseyer/vaporview/1.6.0/file/lramseyer.vaporview-1.6.0.sha256"
)


@pytest.mark.parametrize(
    ("good", "bad", "bad_version", "overrides", "dropped_arg"),
    [
        pytest.param("verible", "sv2v", None, {_SV2V_URL: None}, None, id="download-failure"),
        pytest.param(
            "sv2v", "verible", None, {_VERIBLE_URL: b"tampered"}, None, id="digest-mismatch"
        ),
        pytest.param(
            "sv2v",
            "vaporview",
            None,
            {_VAPORVIEW_SHA_URL: b"0" * 64},
            None,
            id="openvsx-metadata-mismatch",
        ),
        pytest.param("verible", "sv2v", None, {}, "SV2V_SHA256", id="missing-companion-value"),
        pytest.param("verible", "sv2v", "v9.9.9", {}, None, id="version-not-applied"),
        pytest.param(
            "verible",
            "node",
            None,
            {_NODE_INDEX_URL: b'[{"version":"v24.21.0","npm":"1\\nRUN evil"}]'},
            None,
            id="unsafe-upstream-value",
        ),
    ],
)
def test_failure_exits_nonzero_and_leaves_files_unchanged(
    good: str,
    bad: str,
    bad_version: str | None,
    overrides: dict[str, bytes | None],
    dropped_arg: str | None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _checkout(tmp_path, list(_CASES))
    if dropped_arg:
        dockerfile = root / _BASE
        text = re.sub(
            rf"^ARG {dropped_arg}=.*\n", "", dockerfile.read_bytes().decode(), flags=re.M
        )
        dockerfile.write_bytes(text.encode())
    before = _snapshot(root)
    bad_dep, version = _CASES[bad][:2]
    data_file = _data_file(
        tmp_path, [(_CASES[good][0], _CASES[good][1]), (bad_dep, bad_version or version)]
    )

    assert _run(root, data_file, _Upstream(overrides)) == 1

    assert _snapshot(root) == before
    assert bad_dep in capsys.readouterr().err


def test_replace_failure_removes_temp_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(tmp_path, ["sv2v"])
    before = _snapshot(root)

    def refuse(*_args: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "replace", refuse)
    data_file = _data_file(tmp_path, [_CASES["sv2v"][:2]])

    assert _run(root, data_file, _Upstream()) == 1
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "contents", ['{"upgrades": [', '{"upgrades": []}'], ids=["malformed", "empty-upgrades"]
)
def test_bad_data_file_is_a_usage_error(contents: str, tmp_path: Path) -> None:
    root = _checkout(tmp_path, list(_CASES))
    before = _snapshot(root)
    data_file = tmp_path / "upgrades.json"
    data_file.write_text(contents, encoding="utf-8")
    upstream = _Upstream()

    assert _run(root, data_file, upstream) == 2

    assert upstream.requests == []
    assert _snapshot(root) == before


def test_unknown_dep_names_are_ignored(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _checkout(tmp_path, list(_CASES))
    before = _snapshot(root)
    unknown = [("YosysHQ/yosys", "v0.99"), ("openroad", "1.2.3"), ("left-pad", "1.3.0")]
    upstream = _Upstream()

    assert _run(root, _data_file(tmp_path, unknown), upstream) == 0

    assert upstream.requests == []
    assert _snapshot(root) == before
    assert "ignored left-pad" in capsys.readouterr().err
