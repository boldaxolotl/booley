"""Project label identity and the separate stable resource-name namespace."""

import hashlib
import os


def project_label_id(canonical_root: str) -> str:
    """Return the full digest of the exact canonical root spelling."""
    return hashlib.sha256(canonical_root.encode()).hexdigest()


def project_name_id(canonical_root: str) -> str:
    """Preserve the historical normcase short resource-name digest."""
    return project_label_id(os.path.normcase(canonical_root))[:32]


def accepted_project_labels(canonical_root: str) -> tuple[str, str]:
    """Exact root-derived forms accepted by non-destructive readers."""
    return project_label_id(canonical_root), project_name_id(canonical_root)


def matches_project_label(label: str | None, canonical_root: str) -> bool:
    """Recognize canonical or historical labels without accessing the filesystem."""
    return label in accepted_project_labels(canonical_root)
