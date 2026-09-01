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
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from booley.runtime.timefmt import utc_now_rfc3339

logger = logging.getLogger(__name__)

# Bump when the on-disk layout or semantics change in an incompatible way.
# 2.0 stores read-only exact replacement proposals. It deliberately
# invalidates selector-mux locks from 1.x.
LOCK_SCHEMA_VERSION = "2.0"

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class LockMeta:
    """In-memory representation of the schema-2 ``lock.json``."""

    schema_version: str = LOCK_SCHEMA_VERSION
    created_at: str = ""
    scope: list[str] = field(default_factory=list)
    scope_hashes: dict[str, str] = field(default_factory=dict)
    count: int = 0
    mutations: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the schema-2 proposal identity."""
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
            mutations=list(d.get("mutations", [])),
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
