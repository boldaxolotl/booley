"""Structured warning identities and project-local waivers for ``booley doctor``."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

WAIVER_FILENAME = "doctor-waivers.toml"
_CHECK_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:[.-][a-z0-9]+)*$")


class DoctorWaiverError(ValueError):
    """Raised when a Doctor waiver file cannot be trusted."""


class DoctorWarning(str):
    """A warning message carrying a stable check identity and optional subject."""

    check_id: str
    subject: str | None
    dedupe: str | None

    def __new__(
        cls,
        check_id: str,
        message: str,
        *,
        subject: str | None = None,
        dedupe: str | None = None,
    ) -> DoctorWarning:
        _validate_check_id(check_id)
        if subject is not None and not subject.strip():
            raise ValueError("Doctor warning subject must not be empty")
        obj = super().__new__(cls, message)
        obj.check_id = check_id
        obj.subject = subject
        obj.dedupe = dedupe
        return obj


def warning(
    check_id: str,
    message: str,
    *,
    subject: str | None = None,
    dedupe: str | None = None,
) -> DoctorWarning:
    """Construct a warning that can be matched without parsing its prose."""

    return DoctorWarning(check_id, message, subject=subject, dedupe=dedupe)


@dataclass(frozen=True)
class DoctorWaiver:
    """One exact warning waiver loaded from project configuration."""

    check: str
    subject: str | None
    reason: str
    expires: date | None
    permanent: bool

    @property
    def key(self) -> tuple[str, str | None]:
        return self.check, self.subject

    def matches(self, finding: DoctorWarning) -> bool:
        return self.check == finding.check_id and (
            self.subject is None or self.subject == finding.subject
        )


@dataclass
class DoctorWaivers:
    """Validated active and expired waivers plus their match state."""

    path: Path
    active: tuple[DoctorWaiver, ...] = ()
    expired: tuple[DoctorWaiver, ...] = ()
    _used: set[tuple[str, str | None]] = field(default_factory=set, init=False)

    @classmethod
    def empty(cls, path: Path) -> DoctorWaivers:
        return cls(path=path)

    def match(self, finding: DoctorWarning) -> DoctorWaiver | None:
        """Return the most-specific matching waiver and mark it used."""

        ordered = sorted(self.active, key=lambda item: item.subject is None)
        for item in ordered:
            if item.matches(finding):
                self._used.add(item.key)
                return item
        return None

    def unused(self) -> tuple[DoctorWaiver, ...]:
        return tuple(item for item in self.active if item.key not in self._used)


def load_doctor_waivers(project_dir: Path, *, today: date | None = None) -> DoctorWaivers:
    """Load and validate ``doctor-waivers.toml`` from *project_dir*."""

    path = project_dir / WAIVER_FILENAME
    if not path.is_file():
        return DoctorWaivers.empty(path)
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise DoctorWaiverError(f"{path}: {exc}") from exc
    return _parse_waivers(path, data, today=today or date.today())


def _parse_waivers(path: Path, data: dict[str, Any], *, today: date) -> DoctorWaivers:
    unknown = set(data) - {"version", "waiver"}
    if unknown:
        raise DoctorWaiverError(f"{path}: unknown top-level key(s): {', '.join(sorted(unknown))}")
    if type(data.get("version")) is not int or data["version"] != 1:
        raise DoctorWaiverError(f"{path}: version must be the integer 1")
    raw_entries = data.get("waiver", [])
    if not isinstance(raw_entries, list):
        raise DoctorWaiverError(f"{path}: [[waiver]] entries must be an array of tables")

    entries = tuple(_parse_entry(path, index, raw) for index, raw in enumerate(raw_entries, 1))
    keys = [item.key for item in entries]
    if len(keys) != len(set(keys)):
        raise DoctorWaiverError(f"{path}: duplicate check/subject waiver")
    active = tuple(item for item in entries if item.expires is None or item.expires >= today)
    expired = tuple(item for item in entries if item.expires is not None and item.expires < today)
    return DoctorWaivers(path=path, active=active, expired=expired)


def _parse_entry(path: Path, index: int, raw: object) -> DoctorWaiver:
    label = f"{path}: waiver #{index}"
    if not isinstance(raw, dict):
        raise DoctorWaiverError(f"{label} must be a table")
    unknown = set(raw) - {"check", "subject", "reason", "expires", "permanent"}
    if unknown:
        raise DoctorWaiverError(f"{label} has unknown key(s): {', '.join(sorted(unknown))}")

    check = _required_text(raw, "check", label)
    _validate_check_id(check, label=label)
    reason = _required_text(raw, "reason", label)
    subject = raw.get("subject")
    if subject is not None and (not isinstance(subject, str) or not subject.strip()):
        raise DoctorWaiverError(f"{label}.subject must be a non-empty string")

    expires = raw.get("expires")
    permanent = raw.get("permanent", False)
    if not isinstance(permanent, bool):
        raise DoctorWaiverError(f"{label}.permanent must be true or false")
    if expires is not None and type(expires) is not date:
        raise DoctorWaiverError(f"{label}.expires must be a TOML date (YYYY-MM-DD)")
    if permanent == (expires is not None):
        raise DoctorWaiverError(f"{label} must set exactly one of expires or permanent = true")
    return DoctorWaiver(
        check=check,
        subject=subject.strip() if isinstance(subject, str) else None,
        reason=reason,
        expires=expires,
        permanent=permanent,
    )


def _required_text(raw: dict[str, Any], key: str, label: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DoctorWaiverError(f"{label}.{key} must be a non-empty string")
    return value.strip()


def _validate_check_id(check_id: str, *, label: str = "Doctor warning") -> None:
    if not _CHECK_ID_RE.fullmatch(check_id):
        raise DoctorWaiverError(
            f"{label}.check must use lowercase dot/dash-separated identifiers: {check_id!r}"
        )
