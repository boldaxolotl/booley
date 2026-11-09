"""Independent Project identity and naming compatibility checks."""

import hashlib

import pytest

from booley.core import project_identity as identity


def test_identity_bytes_and_deleted_roots(monkeypatch):
    root = "/deleted/Project"
    # Persisted label digests: SHA-256 of the exact root spelling.
    full = "40a9a00ad95eff5ad063a2c00dddc31669d637d22842c9716636e3b378dc09d4"
    assert identity.project_label_id(root) == full
    # The short resource-name digest hashes the normcase spelling.
    monkeypatch.setattr(identity.os.path, "normcase", str.lower)
    short = "812db0e2f3ca36fc8d1babe447c088a2"
    assert identity.project_name_id(root) == short
    assert identity.matches_project_label(full, root)
    assert identity.matches_project_label(short, root)
    # The full digest of the normcase spelling is neither accepted form.
    assert not identity.matches_project_label(
        "812db0e2f3ca36fc8d1babe447c088a2d6808473ec7346da165c0e4cac1b7642", root
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
