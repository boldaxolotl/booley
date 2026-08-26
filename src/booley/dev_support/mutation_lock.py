"""Mutation tester proposal lock and isolated build paths.

The mutation tester writes a *lock* on first cold-start that captures the
creator-agent-designed exact replacement set. Subsequent invocations with
the same scope reuse the proposals, but rebuild the pristine baseline and
each isolated source variant.

Layout under ``$BOOLEY_RUNTIME_DIR/mutation_tester/lock/``::

    lock/
    ├── lock.json
    ├── builds/
    │   ├── baseline/
    │   └── mutant_<N>/
    └── verification_rounds/
        ├── round_1.log          # cold-start sim logs (if retries happened)
        └── ...

The lock is invalidated by: scope set change, any scope file edit, or a
Mutation Tester version bump. Operator override: ``--regen-lock`` wipes the dir.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.runtime.timefmt import utc_now_rfc3339

# re-exported for backward compatibility — the SV source-editing engine was
# extracted into ``mut_harness_inject`` (principle 8 / Single Responsibility),
# but importers still resolve these names off ``mutation_lock`` (see __all__).
from .mut_harness_inject import (
    MUT_ECHO_PREFIX,
    MutHarnessInjectionError,
    generate_mut_pkg,
    generate_plusarg_reader_snippet,
    inject_mut_harness,
    remove_mut_harness,
)

logger = logging.getLogger(__name__)

# Re-exports from ``mut_harness_inject``, kept in the public namespace so
# existing importers of ``mutation_lock`` keep resolving them.
__all__ = [
    "MUT_ECHO_PREFIX",
    "MutHarnessInjectionError",
    "generate_mut_pkg",
    "generate_plusarg_reader_snippet",
    "inject_mut_harness",
    "remove_mut_harness",
]

# Bump when the on-disk layout or semantics change in an incompatible way.
# 2.0 stores read-only exact replacement proposals. It deliberately
# invalidates selector-mux locks from 1.x.
LOCK_SCHEMA_VERSION = "2.0"

# Package + plusarg-reader filename constants (centralised here so other
# layers don't hardcode strings).
MUT_PKG_FILENAME = "booley_mut_pkg.sv"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LockMeta:
    """In-memory representation of ``lock.json``.

    The core proposal fields mirror schema 2. Legacy 1.x attributes remain as
    in-memory compatibility shims but :meth:`to_dict` does not persist them.
    """

    schema_version: str = LOCK_SCHEMA_VERSION
    created_at: str = ""
    scope: list[str] = field(default_factory=list)
    scope_hashes: dict[str, str] = field(default_factory=dict)
    count: int = 0
    host_file: str = ""
    mutations: list[dict[str, Any]] = field(default_factory=list)
    muxed_files: list[str] = field(default_factory=list)
    pkg_file: str = MUT_PKG_FILENAME
    docker_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize only the schema-2 proposal identity.

        Legacy attributes remain on the Python object for callers that still
        construct 1.x fixtures, but they are deliberately absent from new lock
        files: isolated campaigns persist no mux, package, image, or build cache.
        """
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "scope": self.scope,
            "scope_hashes": self.scope_hashes,
            "count": self.count,
            "mutations": self.mutations,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LockMeta:
        # Defensive .get() so a missing field on an old lock is non-fatal at
        # parse time; semantic validity is left to ``is_lock_valid``.
        return cls(
            schema_version=d.get("schema_version", ""),
            created_at=d.get("created_at", ""),
            scope=list(d.get("scope", [])),
            scope_hashes=dict(d.get("scope_hashes", {})),
            count=int(d.get("count", 0)),
            host_file=d.get("host_file", ""),
            mutations=list(d.get("mutations", [])),
            muxed_files=list(d.get("muxed_files", [])),
            pkg_file=d.get("pkg_file", MUT_PKG_FILENAME),
            docker_digest=d.get("docker_digest", ""),
        )


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------


def lock_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the canonical lock directory path.

    Resolution order: explicit ``logs_dir`` -> ``$BOOLEY_RUNTIME_DIR`` ->
    ``$BOOLEY_LOGS_DIR/.runtime`` ->
    fall back to ``./mutation_tester_lock`` (used only by direct callers
    in tests; production always sets BOOLEY_LOGS_DIR).
    """
    if logs_dir is None:
        runtime_env = os.environ.get("BOOLEY_RUNTIME_DIR")
        logs_env = os.environ.get("BOOLEY_LOGS_DIR")
        if runtime_env:
            logs_dir = runtime_env
        elif logs_env:
            logs_dir = str(Path(logs_env) / ".runtime")
        else:
            # Interactive Mode sets neither env var. A cwd-relative
            # ./mutation_tester_lock is not persisted with the project and is
            # not discoverable afterwards, so every interactive run cold-starts
            # the ~4min creator agent and leaves no artifact to inspect
            # (QA_REPORT C2.3). Anchor the lock in the project's own persistent
            # .runtime tree so interactive runs warm-reuse it exactly like
            # Ticket Mode does. Fall back to the relative path only when no
            # project is discoverable (direct test callers).
            try:
                from booley.runtime.project_dir import runtime_dir

                logs_dir = str(runtime_dir())
            except (FileNotFoundError, ImportError):
                logs_dir = "mutation_tester_lock"
    return Path(logs_dir) / "mutation_tester" / "lock"


def build_dir(logs_dir: Path | str | None = None) -> Path:
    """Compatibility alias for the pristine baseline build directory."""
    return baseline_build_dir(logs_dir)


def builds_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the root containing independent baseline and mutant builds."""
    return lock_dir(logs_dir) / "builds"


def baseline_build_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the pristine source build directory."""
    return builds_dir(logs_dir) / "baseline"


def variant_build_dir(index: int, logs_dir: Path | str | None = None) -> Path:
    """Return the build directory for one isolated source replacement."""
    if index < 1:
        raise ValueError("mutation index must be positive")
    return builds_dir(logs_dir) / f"mutant_{index}"


def variants_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the durable exact-source variant artifact directory."""
    return lock_dir(logs_dir) / "variants"


def mutant_logs_dir(logs_dir: Path | str | None = None) -> Path:
    """Return the per-mutant simulator-log directory inside the lock dir.

    Each mutant has its own compiled image, while this directory keeps the
    simulator transcript independent from build-tool layout. A surviving
    (not-detected) mutant is the whole point of the Specialist, and it is exactly the
    case with no failure text to read, so each run's full output is persisted
    here as ``mutant_<mut_id>.log``.
    """
    return lock_dir(logs_dir) / "mutant_logs"


def baseline_log_path(logs_dir: Path | str | None = None) -> Path:
    """Return the current campaign's pristine-baseline simulator log path."""
    return lock_dir(logs_dir) / "baseline.log"


def verification_rounds_dir(logs_dir: Path | str | None = None) -> Path:
    return lock_dir(logs_dir) / "verification_rounds"


def lock_json_path(logs_dir: Path | str | None = None) -> Path:
    return lock_dir(logs_dir) / "lock.json"


def build_meta_path(logs_dir: Path | str | None = None) -> Path:
    return build_dir(logs_dir) / "build_meta.json"


def muxed_path(scope_file: str, logs_dir: Path | str | None = None) -> Path:
    """Translate a scope file path to its muxed copy in the lock dir.

    We use the basename only — scope paths like ``rtl/sub/mod.sv`` and
    ``rtl/mod.sv`` would collide on basename, but that already breaks
    the muxed-file mapping anyway (each scope file maps to one muxed
    sibling).  Keep simple, document the constraint.
    """
    return lock_dir(logs_dir) / "muxed" / _safe_muxed_rel(scope_file)


def _safe_muxed_rel(scope_file: str) -> Path:
    """Return a safe relative path for a muxed lock artifact."""
    raw = str(scope_file).replace("\\", "/")
    parts: list[str] = []
    if re.match(r"^[A-Za-z]:/", raw):
        parts.extend(("abs", raw[0].lower()))
        raw = raw[3:]
    elif raw.startswith("/"):
        parts.append("abs")
        raw = raw.lstrip("/")

    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise ValueError(f"scope path cannot escape lock dir: {scope_file}")
        parts.append(part)
    if not parts:
        raise ValueError("scope path is empty")
    return Path(*parts)


def pkg_path(logs_dir: Path | str | None = None) -> Path:
    return lock_dir(logs_dir) / MUT_PKG_FILENAME


# ---------------------------------------------------------------------------
# Hashing & validity
# ---------------------------------------------------------------------------


def _hash_file(path: Path) -> str:
    """Return ``sha256:<hex>`` digest of *path* contents."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def compute_scope_hashes(scope_files: list[str], work_dir: Path) -> dict[str, str]:
    """Compute sha256 digests for every scope file.

    Missing files map to the literal string ``"sha256:MISSING"`` so callers
    can distinguish "file went away" from "file is empty" (empty hashes to
    a real digest).  This makes ``is_lock_valid`` reject a scope that the
    worktree no longer contains.
    """
    hashes: dict[str, str] = {}
    for rel in scope_files:
        p = Path(rel) if Path(rel).is_absolute() else work_dir / rel
        if p.exists():
            hashes[rel] = _hash_file(p)
        else:
            hashes[rel] = "sha256:MISSING"
    return hashes


def is_lock_valid(
    meta: LockMeta,
    current_scope: list[str],
    current_hashes: dict[str, str],
    current_schema_version: str = LOCK_SCHEMA_VERSION,
) -> bool:
    """Return True when the lock can be reused for the current request.

    Three identity checks, in order of cheapness:
      1. Mutation Tester schema version matches.
      2. Scope set is identical (same files, no additions/removals).
      3. Every scope file's content hash matches the recorded hash.

    Order matters — version mismatch is cheap; hashing is expensive.
    """
    if meta.schema_version != current_schema_version:
        logger.info(
            "mutation lock invalid: schema_version %r != %r",
            meta.schema_version,
            current_schema_version,
        )
        return False
    if set(meta.scope) != set(current_scope):
        logger.info(
            "mutation lock invalid: scope mismatch (locked %s, current %s)",
            sorted(meta.scope),
            sorted(current_scope),
        )
        return False
    for rel in current_scope:
        locked = meta.scope_hashes.get(rel)
        current = current_hashes.get(rel)
        if locked != current:
            logger.info(
                "mutation lock invalid: hash mismatch for %s (%s -> %s)",
                rel,
                locked,
                current,
            )
            return False
    return True


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_lock(logs_dir: Path | str | None = None) -> LockMeta | None:
    """Read ``lock.json`` from disk, or return None if absent/corrupt.

    A corrupt lock is treated as missing — the next cold-start will rebuild.
    """
    path = lock_json_path(logs_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("mutation lock corrupt at %s: %s — treating as missing", path, exc)
        return None
    if not isinstance(data, dict):
        logger.warning("mutation lock at %s is not a JSON object", path)
        return None
    # from_dict coerces fields (int(count), list(scope), ...). A malformed
    # value — e.g. count:"ten" or scope:5 — raises ValueError/TypeError; treat
    # it as a corrupt lock rather than letting it escape this guard.
    try:
        return LockMeta.from_dict(data)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "mutation lock at %s has malformed fields: %s — treating as missing", path, exc
        )
        return None


def save_lock(meta: LockMeta, logs_dir: Path | str | None = None) -> None:
    """Persist *meta* to ``lock.json``, creating parent dirs as needed."""
    path = lock_json_path(logs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(meta.to_dict(), indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def wipe_lock(logs_dir: Path | str | None = None) -> None:
    """Recursively delete the lock dir.  Idempotent."""
    d = lock_dir(logs_dir)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def now_iso() -> str:
    """UTC timestamp suitable for ``LockMeta.created_at``."""
    return utc_now_rfc3339()


# ---------------------------------------------------------------------------
# Build metadata cache
# ---------------------------------------------------------------------------


def save_build_meta(
    muxed_hashes: dict[str, str],
    docker_digest: str,
    build_inputs: dict[str, str] | None = None,
    logs_dir: Path | str | None = None,
) -> None:
    """Record the muxed files, sim inputs, and image snapshot for build reuse."""
    path = build_meta_path(logs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "muxed_hashes": muxed_hashes,
        "build_inputs": build_inputs or {},
        "docker_digest": docker_digest,
        "created_at": now_iso(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def load_build_meta(
    logs_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """Read build_meta.json; None when absent or corrupt."""
    path = build_meta_path(logs_dir)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def is_build_cache_valid(
    muxed_hashes: dict[str, str],
    docker_digest: str,
    build_inputs: dict[str, str] | None = None,
    logs_dir: Path | str | None = None,
) -> bool:
    """True when ``build_meta.json`` matches current muxed files, inputs + image."""
    meta = load_build_meta(logs_dir)
    if meta is None:
        return False
    if meta.get("muxed_hashes") != muxed_hashes:
        return False
    if meta.get("build_inputs") != (build_inputs or {}):
        return False
    return meta.get("docker_digest") == docker_digest


# ---------------------------------------------------------------------------
# SV harness injection (source-editing engine)
# ---------------------------------------------------------------------------
#
# The SystemVerilog source-editing engine (harness text generation + RTL
# rewrite) lives in ``mut_harness_inject`` — extracted per principle 8
# (Single Responsibility).  Its public names are re-exported at module top
# for backward compatibility so existing importers keep resolving them off
# ``mutation_lock``.  See the ``from .mut_harness_inject import ...`` line
# in the import block above.


# ---------------------------------------------------------------------------
# Docker / toolchain digest
# ---------------------------------------------------------------------------


_DIGEST_FILE_CANDIDATES = (
    Path("/etc/booley_image_digest"),
    Path("/opt/booley/image_digest"),
)


def _read_first_digest_file() -> str | None:
    for p in _DIGEST_FILE_CANDIDATES:
        if p.exists():
            try:
                txt = p.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if txt:
                return txt
    return None


def _toolchain_version_string() -> str:
    """Return a stable identifier built from simulator --version output.

    Falls back to ``unknown`` for any EDA tool that isn't on PATH. Both
    Verilator and Icarus are queried so a swap of either invalidates the
    cache.  Output is wrapped in sha256 to keep the digest field short and
    not leak full paths.
    """
    parts: list[str] = []
    for cmd in (["verilator", "--version"], ["iverilog", "-V"]):
        try:
            out = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            parts.append(f"{cmd[0]}: {(out.stdout or out.stderr).strip()}")
        except (OSError, subprocess.SubprocessError):
            parts.append(f"{cmd[0]}: unavailable")
    blob = "\n".join(parts).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def get_docker_digest() -> str:
    """Return a stable identifier for the current sim container/toolchain.

    Preferred source: an explicit digest file written at image build time.
    Fallback: hash of the local Verilator + Icarus version strings.

    Stored in lock.json + build_meta.json so a toolchain swap forces a
    re-elab (per build cache) and a fresh creator round (per lock).
    """
    explicit = _read_first_digest_file()
    if explicit:
        return explicit
    return _toolchain_version_string()


# ---------------------------------------------------------------------------
# DUT top file resolution
# ---------------------------------------------------------------------------


# SV/Verilog comment stripper used before regex-searching for module
# declarations.  Naive — doesn't account for strings — but module headers
# don't sit inside string literals, so the simplification is safe here.
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT_RE = re.compile(r"//[^\n]*")


def _strip_sv_comments(text: str) -> str:
    """Drop block (``/* ... */``) and line (``// ...``) comments from *text*.

    Used so a commented-out ``module foo`` header doesn't masquerade as a
    real declaration when locating the DUT top file.
    """
    text = _BLOCK_COMMENT_RE.sub("", text)
    return _LINE_COMMENT_RE.sub("", text)


def find_dut_top_file(
    dut_top_module: str,
    dut_files: list[str],
    work_dir: Path,
) -> Path | None:
    """Find the file in *dut_files* that declares ``module <dut_top_module>``.

    Parses each candidate's source for a ``module <name>`` declaration
    rather than matching the file basename — a file named ``rtl/foo.sv``
    may legitimately contain ``module foo_v2``, and forcing the basename
    to match the module name has historically driven the coder to split
    sources and leave broken include-stubs behind (which then survive
    into the golden eval).

    Falls back to legacy basename-stem matching ONLY when no candidate
    file is readable or none declares the target module — preserves
    behavior for greenfield runs (files not yet on disk) and for
    test fixtures that elide the actual source text.

    Returns the resolved path (joined with *work_dir* unless absolute),
    or ``None`` when neither strategy locates a candidate.
    """
    if not dut_top_module:
        return None

    resolved: list[Path] = []
    for rel in dut_files:
        p = Path(rel)
        resolved.append(p if p.is_absolute() else work_dir / rel)

    decl_pattern = re.compile(rf"\bmodule\s+{re.escape(dut_top_module)}\b")

    # Primary: source-declaration parse.
    for p in resolved:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if decl_pattern.search(_strip_sv_comments(text)):
            return p

    # Fallback: basename stem match (greenfield + test-fixture path).
    for rel, p in zip(dut_files, resolved, strict=True):
        if Path(rel).stem == dut_top_module:
            return p

    return None
