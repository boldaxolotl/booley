"""Independent Project identity and naming compatibility checks."""

import hashlib

import pytest

from booley.core import project_identity as identity


def test_identity_bytes_and_deleted_roots(monkeypatch):
    root = "/deleted/Project"
    full = hashlib.sha256(root.encode()).hexdigest()
    assert identity.project_label_id(root) == full
    assert identity.project_name_id(root) == full[:32]
    monkeypatch.setattr(identity.os.path, "normcase", str.lower)
    short = hashlib.sha256(root.lower().encode()).hexdigest()[:32]
    assert identity.project_name_id(root) == short
    assert identity.matches_project_label(full, root)
    assert identity.matches_project_label(short, root)
    assert not identity.matches_project_label(
        hashlib.sha256(root.lower().encode()).hexdigest(), root
    )


@pytest.mark.parametrize(
    "label",
    [
        None,
        "",
        "a" * 16,
        "a" * 31,
        "a" * 32,
        "a" * 33,
        "a" * 63,
        "a" * 64,
        "a" * 65,
        hashlib.sha256(b"/other").hexdigest(),
        hashlib.sha256(b"/other").hexdigest()[:32],
    ],
)
def test_rejects_labels_not_derived_from_root(label):
    assert not identity.matches_project_label(label, "/deleted/Project")
