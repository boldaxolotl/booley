#!/usr/bin/env python3
"""Compatibility adapter for the shared commit-policy owner."""

from __future__ import annotations

import sys
from pathlib import Path

try:
    from booley.commit_policy import policy as _policy
except ImportError:
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "booley" / "commit_policy" / "policy.py").is_file():
        sys.path.insert(0, str(source_root))
        from booley.commit_policy import policy as _policy
    else:
        import booley_commit_policy as _policy

StealthPolicy = _policy.StealthPolicy
BANNED_PHRASES = _policy.BANNED_PHRASES
REDACTION_PLACEHOLDER = _policy.REDACTION_PLACEHOLDER
allowed_authors = _policy.allowed_authors
banned_phrases = _policy.banned_phrases
enforce_convention = _policy.enforce_convention
find_banned = _policy.find_banned
has_banned_content = _policy.has_banned_content
identity_allowed = _policy.identity_allowed
max_body_lines = _policy.max_body_lines
redact_banned = _policy.redact_banned
source_checkout_policy_owner = _policy.source_checkout_policy_owner
stealth_enabled = _policy.stealth_enabled
stealth_policy = _policy.stealth_policy

_DEFAULT_BANNED_PHRASES = _policy._DEFAULT_BANNED_PHRASES
_build_banned_res = _policy._build_banned_res
_load_stealth_config = _policy._load_stealth_config
_stealth_section = _policy._stealth_section

__all__ = [
    "BANNED_PHRASES",
    "REDACTION_PLACEHOLDER",
    "StealthPolicy",
    "allowed_authors",
    "banned_phrases",
    "enforce_convention",
    "find_banned",
    "has_banned_content",
    "identity_allowed",
    "max_body_lines",
    "redact_banned",
    "source_checkout_policy_owner",
    "stealth_enabled",
    "stealth_policy",
]
