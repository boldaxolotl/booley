#!/usr/bin/env python3
"""Rewrite the checksums and paired values of a bumped download pin (#2560).

Renovate bumps a pinned version but cannot know the sha256 the image build
verifies, the source tag and commit a VSIX notice cites, or the npm version
bundled with Node. For every upgraded pin this script downloads exactly the
artifact the build downloads for the new version and rewrites those paired
values in ``Dockerfile.base``, ``Dockerfile.riscv``, the ``Tests`` workflow and
the picorv32 demo contract. Version-only copies are left to Renovate's depName
rules; OpenROAD values are manual by decision.

    python3 .github/scripts/pin_checksums.py DATA_FILE [--root CHECKOUT]

``DATA_FILE`` is the update description Renovate writes for a post-upgrade
command, ``{"upgrades": [{"depName": "zachjs/sv2v", "newValue": "v0.0.13"}]}``.
Renovate renders no ``upgrades`` array in its default ``executionMode: "update"``;
use ``executionMode: "branch"`` and a ``dataFileTemplate`` with triple braces
(double braces HTML-escape)::

    {"upgrades":[{{#each upgrades}}{"depName":"{{{depName}}}","newValue":"{{{newValue}}}"}{{#unless @last}},{{/unless}}{{/each}}]}

Pins are known by depName: a GitHub repository (release assets),
``openvsx:<namespace>/<name>`` (VSIX files) or ``node``. Unknown names are
ignored. The files must already carry the new version (Renovate applied the
bump). Exit 0: done, or nothing to do. Exit 1: a download failed, a companion
value is missing, or upstream metadata disagrees with the artifact; no file was
changed. Exit 2: unusable data file.

The URL is read from the build's own download line, so the script hashes what
the build downloads. It fetches only from the hosts each pin already uses plus
``api.github.com`` (release asset digests, VSIX source tags: the API Renovate's
GitHub datasources query). Redirects are followed, as the build's ``curl -fsSL``
does. ``GITHUB_TOKEN``, when set, goes to ``api.github.com`` only (60
unauthenticated requests per hour per IP).

Trust on first download: a recomputed sha256 trusts whatever upstream serves at
that moment. The cross-checks (GitHub release digest, Open VSX ``.sha256``, Node
``SHASUMS256.txt``) only catch upstream metadata disagreeing with its own
artifact, and only where upstream publishes one (sv2v and the RISC-V documents
publish none). Renovate's ``minimumReleaseAge`` and human review of every pin PR
are the only other buffers.

Needs Python 3.11 and ``git`` (importing ``src/booley`` runs ``booley/__init__``).
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Literal

_ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(_ROOT / "src"))

from booley.runtime.dockerfile_syntax import build_arguments

Opener = Callable[[urllib.request.Request], IO[bytes]]

_API = "https://api.github.com"
_CHUNK_BYTES = 1 << 20
_MAX_TAG_PEEL = 5
_ARG_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_RELEASE_URL = re.compile(r"https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)")
# Upstream strings land in Dockerfiles: nothing that could end a line or a quoted string.
_WRITABLE = re.compile(r"[A-Za-z0-9._:/+~-]+")


class PinError(Exception):
    """A pin cannot be rewritten safely; nothing is changed (exit 1)."""


Upgrade = tuple[str, str]  # (depName, newValue)


@dataclass(frozen=True)
class ArgValue:
    """The default of the one ``ARG name=value`` line of a Dockerfile."""

    file: str
    name: str


@dataclass(frozen=True)
class LineValue:
    """Capture group ``group`` of the one match of ``pattern`` in a file."""

    file: str
    pattern: str
    group: int = 1


Value = ArgValue | LineValue


@dataclass(frozen=True)
class Download:
    """One artifact: its URL, and the places its digest and URL are copied to.

    ``url`` is the build's download string with ``${ARG}`` references, which
    must occur once in the pin's version file, or a value holding the literal URL.
    """

    url: str | Value
    sha: tuple[Value, ...]
    url_copies: tuple[Value, ...] = ()


@dataclass(frozen=True)
class Vsix:
    """The Open VSX extension, its source repository, and where its notice cites the tag."""

    extension: str
    repo: str
    tag_format: str
    tag: Value
    commit: Value


@dataclass(frozen=True)
class Pin:
    """Everything the script knows about one pinned download."""

    kind: Literal["release", "openvsx", "node"]
    version: Value
    downloads: tuple[Download, ...]
    hosts: frozenset[str]
    vsix: Vsix | None = None
    npm: Value | None = None

    @property
    def values(self) -> tuple[Value, ...]:
        """Every location the script reads or writes for this pin."""
        found: list[Value] = [self.version]
        for download in self.downloads:
            found += [*download.sha, *download.url_copies]
            found += [download.url] if not isinstance(download.url, str) else []
        found += [self.vsix.tag, self.vsix.commit] if self.vsix else []
        found += [self.npm] if self.npm else []
        return tuple(found)


_BASE = "src/booley/data/docker/Dockerfile.base"
_RISCV = "src/booley/data/docker/Dockerfile.riscv"
_WORKFLOW = ".github/workflows/test.yml"
_DEMO = ".github/contracts/picorv32-demo.toml"
_GITHUB = frozenset({"github.com", "api.github.com"})
_OPEN_VSX = frozenset({"open-vsx.org", "api.github.com"})
_ACTIONLINT_URL = (
    r"(https://github\.com/rhysd/actionlint/releases/download/v([0-9][^/\s]*)"
    r"/actionlint_\2_linux_amd64\.tar\.gz)"
)
_XPACK = "xpack-dev-tools/riscv-none-elf-gcc-xpack"
_XPACK_FILE = "xpack-riscv-none-elf-gcc-${XPACK_GCC_VERSION}-linux-x64.tar.gz"


def _release(
    file: str, version_arg: str, repo: str, *assets: tuple[str, str], tag: str = ""
) -> Pin:
    """A GitHub release pin: each asset is ``(file name, ARG holding its sha256)``."""
    tag = tag or f"${{{version_arg}}}"
    base = f"https://github.com/{repo}/releases/download/{tag}"
    downloads = tuple(Download(f"{base}/{name}", (ArgValue(file, sha),)) for name, sha in assets)
    return Pin("release", ArgValue(file, version_arg), downloads, _GITHUB)


def _vsix(prefix: str, extension: str, repo: str, tag_format: str) -> Pin:
    namespace, name = extension.split("/")
    version = f"${{{prefix}_VERSION}}"
    url = f"https://open-vsx.org/api/{extension}/{version}/file/{namespace}.{name}-{version}.vsix"

    def arg(suffix: str) -> ArgValue:
        return ArgValue(_BASE, f"{prefix}_{suffix}")

    vsix = Vsix(extension, repo, tag_format, arg("SOURCE_TAG"), arg("SOURCE_COMMIT"))
    return Pin("openvsx", arg("VERSION"), (Download(url, (arg("SHA256"),)),), _OPEN_VSX, vsix)


PINS: dict[str, Pin] = {
    "zachjs/sv2v": _release(
        _BASE, "SV2V_VERSION", "zachjs/sv2v", ("sv2v-Linux.zip", "SV2V_SHA256")
    ),
    "chipsalliance/verible": _release(
        _BASE,
        "VERIBLE_VERSION",
        "chipsalliance/verible",
        ("verible-${VERIBLE_VERSION}-linux-static-x86_64.tar.gz", "VERIBLE_SHA256"),
    ),
    _XPACK: Pin(
        "release",
        ArgValue(_RISCV, "XPACK_GCC_VERSION"),
        (
            Download(
                f"https://github.com/{_XPACK}/releases/download/v${{XPACK_GCC_VERSION}}/{_XPACK_FILE}",
                (
                    ArgValue(_RISCV, "XPACK_GCC_SHA256"),
                    LineValue(_DEMO, r'^toolchain_sha256 = "(\S+)"$'),
                ),
                (LineValue(_DEMO, r'^toolchain_url = "(\S+)"$'),),
            ),
        ),
        _GITHUB,
    ),
    "riscv/riscv-isa-manual": _release(
        _RISCV,
        "ISA_MANUAL_RELEASE",
        "riscv/riscv-isa-manual",
        *(
            (
                f"riscv-{kind}-${{ISA_MANUAL_RELEASE}}.{ext}",
                f"ISA_{kind.upper()}_{ext.upper()}_SHA256",
            )
            for kind in ("unprivileged", "privileged")
            for ext in ("pdf", "html")
        ),
    ),
    "riscv/riscv-debug-spec": _release(
        _RISCV,
        "DEBUG_SPEC_RELEASE",
        "riscv/riscv-debug-spec",
        ("riscv-debug-specification.pdf", "DEBUG_SPEC_SHA256"),
    ),
    "riscv-non-isa/riscv-elf-psabi-doc": _release(
        _RISCV,
        "PSABI_RELEASE",
        "riscv-non-isa/riscv-elf-psabi-doc",
        ("riscv-abi.pdf", "PSABI_SHA256"),
    ),
    "rhysd/actionlint": Pin(
        "release",
        LineValue(_WORKFLOW, _ACTIONLINT_URL, 2),
        (
            Download(
                LineValue(_WORKFLOW, _ACTIONLINT_URL, 1),
                (LineValue(_WORKFLOW, r"^ +ACTIONLINT_SHA256: (\S+)$"),),
            ),
        ),
        _GITHUB,
    ),
    "openvsx:lramseyer/vaporview": _vsix(
        "VAPORVIEW", "lramseyer/vaporview", "Lramseyer/vaporview", "{version}-rel"
    ),
    "openvsx:mshr-h/veriloghdl": _vsix(
        "VERILOGHDL", "mshr-h/veriloghdl", "mshr-h/vscode-verilog-hdl-support", "v{version}"
    ),
    "openvsx:ms-vscode/live-server": _vsix(
        "LIVE_PREVIEW", "ms-vscode/live-server", "microsoft/vscode-livepreview", "v{version}"
    ),
    "node": Pin(
        "node",
        ArgValue(_BASE, "NODE_VERSION"),
        (
            Download(
                "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz",
                (ArgValue(_BASE, "NODE_SHA256"),),
            ),
        ),
        frozenset({"nodejs.org"}),
        npm=ArgValue(_BASE, "NPM_VERSION"),
    ),
}


class Tree:
    """The pinned files, read once and edited in memory until ``changed`` is published."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._original: dict[str, str] = {}
        self._texts: dict[str, str] = {}

    def text(self, file: str) -> str:
        if file not in self._texts:
            try:
                self._original[file] = self._texts[file] = (self.root / file).read_bytes().decode()
            except (OSError, UnicodeDecodeError) as error:
                raise PinError(f"cannot read {file}: {error}") from error
        return self._texts[file]

    def changed(self) -> list[str]:
        return [file for file, text in self._texts.items() if text != self._original[file]]

    def read(self, value: Value) -> str:
        start, end = self._span(value)
        return self.text(value.file)[start:end]

    def write(self, value: Value, new: str) -> None:
        if not _WRITABLE.fullmatch(new):
            raise PinError(f"refusing to write unexpected value {new!r} to {value.file}")
        start, end = self._span(value)
        text = self.text(value.file)
        self._texts[value.file] = text[:start] + new + text[end:]

    def url(self, download: Download, file: str) -> str:
        """Return the URL the build downloads; a template must be in ``file`` exactly once."""
        if not isinstance(download.url, str):
            return self.read(download.url)
        if self.text(file).count(download.url) != 1:
            raise PinError(f"{file}: the download {download.url} is not there exactly once")
        return _ARG_REFERENCE.sub(
            lambda match: self.read(ArgValue(file, match.group(1))), download.url
        )

    def _span(self, value: Value) -> tuple[int, int]:
        text = self.text(value.file)
        if isinstance(value, LineValue):
            matches = list(re.finditer(value.pattern, text, re.MULTILINE))
            if len(matches) != 1:
                raise PinError(f"{value.file}: {len(matches)} matches of {value.pattern!r}, not 1")
            return matches[0].span(value.group)
        try:
            found = [a for a in build_arguments(text) if a.name == value.name and a.default]
        except ValueError as error:
            raise PinError(f"{value.file}: {error}") from error
        if len(found) != 1:
            raise PinError(f"{value.file}: ARG {value.name} has {len(found)} defaults, not 1")
        lines = text.splitlines(keepends=True)
        prefix = f"ARG {value.name}="
        if lines[found[0].line - 1].rstrip("\r\n") != f"{prefix}{found[0].default}":
            raise PinError(f"{value.file}:{found[0].line}: unsupported ARG form")
        start = sum(len(line) for line in lines[: found[0].line - 1]) + len(prefix)
        return start, start + len(found[0].default or "")


class Upstream:
    """Reads from the network through ``opener``, only from hosts a pin may use."""

    def __init__(self, opener: Opener | None = None, token: str | None = None) -> None:
        self._opener = opener or (lambda request: urllib.request.urlopen(request, timeout=120))
        self._token = token or None
        self._json: dict[str, Any] = {}

    def sha256(self, url: str, hosts: frozenset[str]) -> str:
        """Stream ``url`` through sha256 and return the hex digest."""

        def digest(response: IO[bytes]) -> str:
            hasher = hashlib.sha256()
            for chunk in iter(lambda: response.read(_CHUNK_BYTES), b""):
                hasher.update(chunk)
            return hasher.hexdigest()

        return self._read(url, hosts, digest)

    def text(self, url: str, hosts: frozenset[str]) -> str:
        return self._read(url, hosts, lambda response: response.read().decode())

    def json_at(self, url: str, hosts: frozenset[str]) -> Any:
        if url not in self._json:
            self._json[url] = self._read(url, hosts, lambda response: json.loads(response.read()))
        return self._json[url]

    def _read(self, url: str, hosts: frozenset[str], consume: Callable[[IO[bytes]], Any]) -> Any:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme != "https" or parts.hostname not in hosts:
            raise PinError(f"refusing to fetch {url}: not a host this pin already uses")
        request = urllib.request.Request(url, headers={"User-Agent": "booley-pin-checksums"})
        if self._token and parts.hostname == "api.github.com":
            request.add_unredirected_header("Authorization", f"Bearer {self._token}")
        try:
            with self._opener(request) as response:
                return consume(response)
        except (OSError, http.client.HTTPException, ValueError) as error:
            raise PinError(f"cannot fetch {url}: {error}") from error


def _get(data: Any, *path: str) -> Any:
    """Walk ``path`` through nested JSON objects; a missing key is a bad upstream response."""
    for key in path:
        if not isinstance(data, dict) or key not in data:
            raise PinError(f"upstream response lacks {'.'.join(path)}")
        data = data[key]
    return data


Write = tuple[Value, str]


def _check_release(pin: Pin, tree: Tree, url: str, digest: str, up: Upstream) -> list[Write]:
    """Cross-check the digest GitHub publishes for the release asset, when it publishes one."""
    match = _RELEASE_URL.fullmatch(url)
    if match is None:
        raise PinError(f"{url} is not a GitHub release asset")
    repo, tag, name = match.groups()
    release = up.json_at(f"{_API}/repos/{repo}/releases/tags/{tag}", pin.hosts)
    assets = [asset for asset in _get(release, "assets") if _get(asset, "name") == name]
    if not assets:
        raise PinError(f"release {tag} of {repo} has no asset {name}")
    published = assets[0].get("digest")
    if published is not None and published != f"sha256:{digest}":
        raise PinError(f"{name}: downloaded sha256 {digest} but the release publishes {published}")
    return []


def _tag_commit(up: Upstream, hosts: frozenset[str], repo: str, tag: str) -> str:
    """Resolve a tag to its commit, peeling annotated tags."""
    target = _get(up.json_at(f"{_API}/repos/{repo}/git/ref/tags/{tag}", hosts), "object")
    for _ in range(_MAX_TAG_PEEL):
        kind, sha = _get(target, "type"), _get(target, "sha")
        if kind == "commit":
            return sha
        if kind != "tag":
            break
        target = _get(up.json_at(f"{_API}/repos/{repo}/git/tags/{sha}", hosts), "object")
    raise PinError(f"tag {tag} of {repo} does not resolve to a commit")


def _repository_key(url: str) -> str:
    return url.lower().removeprefix("git+").removesuffix("/").removesuffix(".git")


def _check_openvsx(pin: Pin, tree: Tree, url: str, digest: str, up: Upstream) -> list[Write]:
    """Cross-check Open VSX metadata and resolve the source tag and commit."""
    vsix = pin.vsix
    assert vsix is not None, "an openvsx pin always carries a Vsix"
    version = tree.read(pin.version)
    meta = up.json_at(f"https://open-vsx.org/api/{vsix.extension}/{version}", pin.hosts)
    if _get(meta, "version") != version or _get(meta, "files", "download") != url:
        raise PinError(f"Open VSX describes a different file than the build downloads ({url})")
    repository = _get(meta, "repository")
    if _repository_key(repository) != _repository_key(f"https://github.com/{vsix.repo}"):
        raise PinError(f"Open VSX points at {repository}, not {vsix.repo}")
    published = (up.text(_get(meta, "files", "sha256"), pin.hosts).split() or [""])[0].lower()
    if published != digest:
        raise PinError(f"downloaded sha256 {digest} but Open VSX publishes {published!r}")
    tag = vsix.tag_format.format(version=version)
    return [(vsix.tag, tag), (vsix.commit, _tag_commit(up, pin.hosts, vsix.repo, tag))]


def _check_node(pin: Pin, tree: Tree, url: str, digest: str, up: Upstream) -> list[Write]:
    """Cross-check ``SHASUMS256.txt`` and read the npm version bundled with this Node."""
    base, _, name = url.rpartition("/")
    sums = up.text(f"{base}/SHASUMS256.txt", pin.hosts)
    published = dict(line.split()[::-1] for line in sums.splitlines() if len(line.split()) == 2)
    if published.get(name) != digest:
        raise PinError(
            f"downloaded sha256 {digest} but SHASUMS256.txt lists {published.get(name)}"
        )
    index = up.json_at("https://nodejs.org/dist/index.json", pin.hosts)
    release = f"v{tree.read(pin.version).removeprefix('v')}"
    npm = [_get(entry, "npm") for entry in index if _get(entry, "version") == release]
    if len(npm) != 1:
        raise PinError(f"dist/index.json lists {len(npm)} entries for {release}")
    assert pin.npm is not None, "a node pin always names its npm version"
    return [(pin.npm, npm[0])]


_CHECKS = {"release": _check_release, "openvsx": _check_openvsx, "node": _check_node}


def _apply(pin: Pin, new_value: str, tree: Tree, up: Upstream) -> None:
    """Recompute every paired value of ``pin`` from upstream and queue it in ``tree``."""
    version = tree.read(pin.version)
    if version.removeprefix("v") != new_value.removeprefix("v"):
        raise PinError(f"the files pin {version} but the upgrade is to {new_value}")
    writes: list[Write] = []
    for download in pin.downloads:
        url = tree.url(download, pin.version.file)
        digest = up.sha256(url, pin.hosts)
        writes += [(value, digest) for value in download.sha]
        writes += [(value, url) for value in download.url_copies]
        writes += _CHECKS[pin.kind](pin, tree, url, digest, up)
    for value, new in writes:
        tree.write(value, new)


def _publish(tree: Tree) -> list[Path]:
    """Write every changed file: all temp files exist before the first replace."""
    pending: list[tuple[Path, Path]] = []
    try:
        for file in tree.changed():
            target = tree.root / file
            temp = target.with_name(f".{target.name}.pin-checksums.tmp")
            pending.append((temp, target))
            temp.write_bytes(tree.text(file).encode())
            shutil.copymode(target, temp)
        for temp, target in pending:
            temp.replace(target)
    except OSError as error:
        for temp, _ in pending:
            temp.unlink(missing_ok=True)
        raise PinError(f"cannot write the rewritten files: {error}") from error
    return [target for _, target in pending]


def rewrite(root: Path, upgrades: Sequence[Upgrade], upstream: Upstream) -> list[Path]:
    """Rewrite the paired values of every known upgrade; return the files changed.

    Nothing is written unless every known upgrade succeeded.
    """
    tree = Tree(root)
    for dep_name, new_value in upgrades:
        pin = PINS.get(dep_name)
        if pin is None:
            print(f"ignored {dep_name}", file=sys.stderr)
            continue
        try:
            _apply(pin, new_value, tree, upstream)
        except PinError as error:
            raise PinError(f"{dep_name}: {error}") from error
    return _publish(tree)


def _load_upgrades(path: Path) -> list[Upgrade]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    entries = data.get("upgrades") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path}: no 'upgrades' list (Renovate needs executionMode 'branch')")
    upgrades: list[Upgrade] = []
    for entry in entries:
        names = (entry.get("depName"), entry.get("newValue")) if isinstance(entry, dict) else ()
        if len(names) != 2 or not all(isinstance(name, str) and name for name in names):
            raise ValueError(f"{path}: every upgrade needs a depName and a newValue")
        upgrades.append((names[0], names[1]))
    return upgrades


def main(argv: Sequence[str] | None = None, *, opener: Opener | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rewrite pin checksums after a Renovate bump.")
    parser.add_argument("data_file", type=Path, help="Renovate update description (JSON)")
    parser.add_argument("--root", type=Path, default=_ROOT, help="checkout to rewrite")
    args = parser.parse_args(argv)
    try:
        upgrades = _load_upgrades(args.data_file)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    try:
        changed = rewrite(args.root, upgrades, Upstream(opener, os.environ.get("GITHUB_TOKEN")))
    except PinError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for path in changed:
        print(f"rewrote {path.relative_to(args.root).as_posix()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
