"""Fetch and validate the optional Nangate45 data used by ASIC synthesis.

Booley deliberately does not redistribute these files.  ``booley init``
downloads a pinned upstream revision into the user's Booley config directory,
verifies every file by SHA-256, and mounts that directory read-only into the
Sandbox at :data:`CONTAINER_ROOT`.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import ssl
import stat
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import BinaryIO

from booley.runtime.auth_token import config_dir

REVISION = "a008522d88b669ac4c985609533cf5a3d2649222"
CONTAINER_ROOT = "/opt/pdk"
LICENSE_ID = "LicenseRef-Nangate-OCL-1.0"
LICENSE_FILENAME = "Nangate-OCL-1.0.txt"
SOURCE_MANIFEST = "SOURCE.json"
_BASE_URL = f"https://raw.githubusercontent.com/The-OpenROAD-Project/OpenROAD/{REVISION}"
_USER_AGENT = "Booley-Nangate-Setup/0.2"
# raw.githubusercontent.com occasionally times out or answers 5xx; the files are
# pinned by checksum, so repeating a download is safe. Waits double each retry.
_DOWNLOAD_ATTEMPTS = 3
_RETRY_BASE_DELAY_SECONDS = 2.0
# Client errors that mean "try again later" rather than "this request is wrong".
_RETRYABLE_CLIENT_STATUSES = frozenset({408, 429})


class NangatePdkError(RuntimeError):
    """The pinned Nangate45 cache could not be prepared safely."""


@dataclass(frozen=True)
class PinnedFile:
    """One upstream file and its installed path below the PDK root."""

    source: str
    destination: str
    sha256: str

    @property
    def url(self) -> str:
        return f"{_BASE_URL}/{self.source}"


FILES = (
    PinnedFile(
        "test/Nangate45/Nangate45_typ.lib",
        "cell/lib/NangateOpenCellLibrary_typical_ccs.lib",
        "2efd0b32eb580e4e60e72fc0575bb3bc69aac907c91d908442e4ae6d7fe55895",
    ),
    PinnedFile(
        "test/Nangate45/Nangate45_stdcell.lef",
        "nangate45/Nangate45_stdcell.lef",
        "5fdaf0a12102a969d349ed086b2b7106e8332ae4620c66788d52b0a3f8131e62",
    ),
    PinnedFile(
        "test/Nangate45/Nangate45_tech.lef",
        "nangate45/Nangate45_tech.lef",
        "834a79295054cd4209178d1bade67c353863c47bb4b3c22ee38b862b7cec37f2",
    ),
    PinnedFile(
        "test/Nangate45/Nangate45.rc",
        "nangate45/Nangate45.rc",
        "0ece6b835601a7b2cab08a2ef638befd6f6f70c86f1ad2a683489713e5717e09",
    ),
)

OpenUrl = Callable[[urllib.request.Request, float], BinaryIO]
Sleep = Callable[[float], None]


def cache_root() -> Path:
    """Absolute host path of this revision's user-owned PDK cache."""
    return (config_dir() / "pdk" / f"nangate45-{REVISION[:12]}").absolute()


def secure_config_dir_for_cache(root: Path) -> bool:
    """Secure the shared config root when *root* is Booley's default cache."""
    directory = config_dir().absolute()
    if root.absolute() != cache_root():
        return False
    if directory.is_symlink():
        raise NangatePdkError(f"Booley config directory must not be a symlink: {directory}")
    try:
        existed = directory.exists()
        previous_mode = stat.S_IMODE(directory.stat().st_mode) if existed else None
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = directory.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise NangatePdkError(f"Booley config path is not a directory: {directory}")
        if os.name != "nt":
            if info.st_uid != os.getuid():
                raise NangatePdkError(f"Booley config directory is not user-owned: {directory}")
            directory.chmod(0o700)
    except OSError as exc:
        raise NangatePdkError(
            f"could not secure Booley config directory {directory}: {exc}"
        ) from exc
    return not existed or (os.name != "nt" and previous_mode != 0o700)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _license_bytes() -> bytes:
    return resources.files("booley").joinpath("data", "licenses", LICENSE_FILENAME).read_bytes()


@dataclass(frozen=True)
class CacheInspection:
    """Separate cache work Bootstrap can perform from inaccessible state."""

    pending: tuple[str, ...]
    errors: tuple[str, ...]


def _inspect_member(root: Path, item: PinnedFile) -> CacheInspection:
    path = root / item.destination
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            return CacheInspection(
                (), (f"cache file conflicts with nonregular path {path}; remove or rename {path}",)
            )
        actual = _sha256(path)
    except FileNotFoundError:
        return CacheInspection((f"missing {item.destination}",), ())
    except OSError as exc:
        return CacheInspection((), (f"cannot read {path}: {exc}; restore read access to {path}",))
    pending = (f"checksum mismatch for {item.destination}",) if actual != item.sha256 else ()
    return CacheInspection(pending, ())


def _inspect_license(root: Path) -> CacheInspection:
    path = root / LICENSE_FILENAME
    try:
        actual = path.read_bytes()
    except FileNotFoundError:
        return CacheInspection((f"missing or changed {LICENSE_FILENAME}",), ())
    except OSError as exc:
        return CacheInspection((), (f"cannot read {path}: {exc}; restore read access to {path}",))
    try:
        expected = _license_bytes()
    except OSError as exc:
        return CacheInspection(
            (), (f"cannot read packaged {LICENSE_FILENAME}: {exc}; reinstall Booley",)
        )
    pending = (f"missing or changed {LICENSE_FILENAME}",) if actual != expected else ()
    return CacheInspection(pending, ())


def inspect_cache(root: Path | None = None) -> CacheInspection:
    """Inspect every pinned file without hiding permission errors as pending."""
    root = root or cache_root()
    results = (*(_inspect_member(root, item) for item in FILES), _inspect_license(root))
    return CacheInspection(
        tuple(message for result in results for message in result.pending),
        tuple(message for result in results for message in result.errors),
    )


def validation_errors(root: Path | None = None) -> tuple[str, ...]:
    """Return every missing, checksum-mismatched, or unreadable cache member."""
    root = root or cache_root()
    errors: list[str] = []
    for item in FILES:
        path = root / item.destination
        if not path.is_file():
            errors.append(f"missing {item.destination}")
            continue
        try:
            actual = _sha256(path)
        except OSError as exc:
            errors.append(f"cannot read {item.destination}: {exc}")
            continue
        if actual != item.sha256:
            errors.append(f"checksum mismatch for {item.destination}")
    license_path = root / LICENSE_FILENAME
    try:
        if license_path.read_bytes() != _license_bytes():
            errors.append(f"missing or changed {LICENSE_FILENAME}")
    except OSError:
        errors.append(f"missing or changed {LICENSE_FILENAME}")
    return tuple(errors)


def is_ready(root: Path | None = None) -> bool:
    """Whether the complete pinned cache exists and passes verification."""
    return not validation_errors(root)


def _open_url(request: urllib.request.Request, timeout: float) -> BinaryIO:
    return urllib.request.urlopen(request, timeout=timeout)


def _download_once(item: PinnedFile, target: Path, opener: OpenUrl) -> str:
    """Write one complete response body to *target* and return its SHA-256."""
    request = urllib.request.Request(item.url, headers={"User-Agent": _USER_AGENT})
    digest = hashlib.sha256()
    with opener(request, 60.0) as response, target.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    return digest.hexdigest()


def _is_transient(exc: OSError | http.client.HTTPException) -> bool:
    """Whether a failed download may succeed when repeated.

    Network failures may. A 4xx refusal, an untrusted certificate, and a local
    write error (a full disk, a denied path) will not, so they fail at once.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code in _RETRYABLE_CLIENT_STATUSES
    cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(cause, ssl.SSLCertVerificationError):
        return False
    transient = (
        urllib.error.URLError,
        TimeoutError,
        ConnectionError,
        ssl.SSLError,
        http.client.HTTPException,
    )
    return isinstance(exc, transient)


def _download(item: PinnedFile, target: Path, opener: OpenUrl, sleep: Sleep) -> None:
    """Download and verify *item*, retrying transient failures with exponential backoff."""
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        try:
            digest = _download_once(item, target, opener)
        # URLError and socket timeouts are OSErrors; a truncated body is an HTTPException.
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()  # an HTTP error response holds its body and connection open
            if attempt == _DOWNLOAD_ATTEMPTS or not _is_transient(exc):
                raise NangatePdkError(
                    f"could not download {item.url} (attempt {attempt} of "
                    f"{_DOWNLOAD_ATTEMPTS}): {exc}"
                ) from exc
            sleep(_RETRY_BASE_DELAY_SECONDS * 2 ** (attempt - 1))
            continue
        # The content is pinned, so a mismatch is not transient: never retry it.
        if digest != item.sha256:
            raise NangatePdkError(f"downloaded checksum mismatch for {item.source}")
        return


def _manifest_bytes() -> bytes:
    document = {
        "license": LICENSE_ID,
        "revision": REVISION,
        "source_repository": "https://github.com/The-OpenROAD-Project/OpenROAD",
        "files": [
            {
                "destination": item.destination,
                "sha256": item.sha256,
                "url": item.url,
            }
            for item in FILES
        ],
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode()


def fetch(
    root: Path | None = None, *, opener: OpenUrl = _open_url, sleep: Sleep = time.sleep
) -> Path:
    """Download, verify, then atomically replace each pinned file in *root*.

    A download that fails transiently is retried with backoff (see ``_download``).
    """
    root = root or cache_root()
    secure_config_dir_for_cache(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix=".nangate45-", dir=root.parent) as temporary:
            stage = Path(temporary)
            for item in FILES:
                target = stage / item.destination
                target.parent.mkdir(parents=True, exist_ok=True)
                _download(item, target, opener, sleep)
            (stage / LICENSE_FILENAME).write_bytes(_license_bytes())
            (stage / SOURCE_MANIFEST).write_bytes(_manifest_bytes())

            root.mkdir(parents=True, exist_ok=True)
            for item in FILES:
                destination = root / item.destination
                destination.parent.mkdir(parents=True, exist_ok=True)
                (stage / item.destination).replace(destination)
            for name in (LICENSE_FILENAME, SOURCE_MANIFEST):
                (stage / name).replace(root / name)
    except NangatePdkError:
        raise
    except OSError as exc:
        raise NangatePdkError(f"could not install Nangate45 cache at {root}: {exc}") from exc

    errors = validation_errors(root)
    if errors:
        raise NangatePdkError("installed Nangate45 cache failed validation: " + "; ".join(errors))
    return root
