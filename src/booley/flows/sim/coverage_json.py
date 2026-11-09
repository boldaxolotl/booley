"""The one JSON helper set shared by every coverage module.

Freeze and thaw, plain-JSON conversion, the ``sha256:`` content digest and each
persisted canonical byte form live here. Every canonical form is a named
:class:`CanonicalJsonForm` with all of its options spelled out, so a stored form
changes only when its own entry changes; ``tests/flows/sim/test_coverage_canonical_forms.py``
pins the bytes of each.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TypeAlias

JsonScalar: TypeAlias = str | int | float | bool | None
FrozenJson: TypeAlias = JsonScalar | tuple["FrozenJson", ...] | Mapping[str, "FrozenJson"]

SHA256_PREFIX = "sha256:"


def _freeze(value: object) -> FrozenJson:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(value[key]) for key in sorted(value, key=str)})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, str | int | float | bool):
        return value
    raise TypeError(f"coverage data contains non-JSON value {type(value).__name__}")


def freeze_coverage_mapping(value: object) -> Mapping[str, FrozenJson]:
    """Deeply freeze one JSON object: sorted string keys, tuples for arrays."""
    frozen = _freeze(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("coverage data must be a JSON object")
    return frozen


def coverage_mapping_document(value: Mapping[str, object]) -> dict[str, object]:
    """Convert frozen or plain JSON to plain dicts and lists, keeping key order."""
    return {key: _plain(item) for key, item in value.items()}


def _plain(value: object) -> object:
    if isinstance(value, Mapping):
        return coverage_mapping_document(value)
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def content_digest(data: bytes) -> str:
    """Return the ``sha256:``-prefixed hex digest of ``data``."""
    return SHA256_PREFIX + hashlib.sha256(data).hexdigest()


def _mapping_as_dict(value: object) -> object:
    """``json.dumps`` hook: encode frozen mappings; reject every other non-JSON value."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


@dataclass(frozen=True, slots=True)
class CanonicalJsonForm:
    """One persisted canonical JSON byte form; every option is required."""

    ensure_ascii: bool
    allow_nan: bool
    #: ``None`` is compact (``","`` and ``":"``); an int pretty-prints with that indent.
    indent: int | None
    trailing_newline: bool

    def encode(self, value: object) -> bytes:
        """Encode ``value`` as UTF-8 JSON with sorted keys in this form."""
        text = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=self.ensure_ascii,
            allow_nan=self.allow_nan,
            indent=self.indent,
            separators=(",", ":") if self.indent is None else (",", ": "),
            default=_mapping_as_dict,
        )
        return text.encode("utf-8") + (b"\n" if self.trailing_newline else b"")


#: Coverage Point id payload, written by the Campaign codec and the collector.
POINT_ID_JSON = CanonicalJsonForm(
    ensure_ascii=False, allow_nan=True, indent=None, trailing_newline=False
)
#: One line of the gzip JSONL point store.
POINT_STORE_LINE_JSON = CanonicalJsonForm(
    ensure_ascii=False, allow_nan=False, indent=None, trailing_newline=True
)
#: The Campaign manifest, ``coverage.json``.
MANIFEST_JSON = CanonicalJsonForm(
    ensure_ascii=True, allow_nan=False, indent=2, trailing_newline=True
)
#: A Coverage Campaign reference document.
REFERENCE_JSON = CanonicalJsonForm(
    ensure_ascii=False, allow_nan=False, indent=None, trailing_newline=True
)
#: The projection hashed into the Approved Waiver Set digest.
WAIVER_SET_JSON = CanonicalJsonForm(
    ensure_ascii=False, allow_nan=True, indent=None, trailing_newline=False
)
#: The payload hashed into the criterion fingerprint.
CRITERION_FINGERPRINT_JSON = CanonicalJsonForm(
    ensure_ascii=True, allow_nan=True, indent=None, trailing_newline=False
)
#: A value hashed into a Coverage Campaign fingerprint.
CAMPAIGN_FINGERPRINT_JSON = CanonicalJsonForm(
    ensure_ascii=True, allow_nan=True, indent=None, trailing_newline=False
)
