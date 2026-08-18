"""Paid-seat evidence helpers used only by administrator-gated tests."""

from __future__ import annotations

import re
from dataclasses import dataclass

_FEATURE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


class LicenseEvidenceError(RuntimeError):
    """A fixed vendor status query does not prove the required seat state."""


@dataclass(frozen=True)
class FlexnetStatus:
    """One exact feature block observed from vendor ``lmutil lmstat`` output."""

    feature: str
    total_in_use: int
    client_observed: bool


def parse_flexnet_lmstat(
    text: str,
    expected_feature: str,
    expected_client_host: str,
) -> FlexnetStatus:
    """Parse an exact client row from host-captured vendor server status.

    The caller must obtain *text* by directly executing the fixed absolute
    ``lmutil`` from the read-only enrolled Vivado mount. Project logs and Flow
    stdout are intentionally not accepted by this API.
    """
    if _FEATURE_RE.fullmatch(expected_feature) is None:
        raise LicenseEvidenceError("expected FlexNet feature has an invalid spelling")
    if (
        not expected_client_host
        or any(char.isspace() for char in expected_client_host)
        or len(expected_client_host) > 255
    ):
        raise LicenseEvidenceError("expected FlexNet client host is invalid")
    header = re.compile(
        rf"^Users of {re.escape(expected_feature)}:\s*"
        r"\(Total of \d+ licenses? issued;\s*"
        r"Total of (\d+) licenses? in use\)\s*$",
        re.M,
    )
    match = header.search(text)
    if match is None:
        raise LicenseEvidenceError(
            f"FlexNet status has no exact {expected_feature!r} feature block"
        )
    start = match.end()
    next_header = re.search(r"^Users of .+?:", text[start:], re.M)
    end = start + next_header.start() if next_header is not None else len(text)
    block = text[start:end]
    quoted_feature = re.search(
        rf'^\s*"{re.escape(expected_feature)}"\s+v\S+,\s+vendor:\s*\S+',
        block,
        re.M,
    )
    if quoted_feature is None:
        raise LicenseEvidenceError("FlexNet feature block lacks vendor metadata")
    client_token = re.compile(rf"(?<!\S){re.escape(expected_client_host)}(?!\S)")
    observed = any(
        client_token.search(line) is not None
        for line in block.splitlines()
        if line.strip() and not line.lstrip().startswith('"')
    )
    total = int(match.group(1))
    if observed and total < 1:
        raise LicenseEvidenceError("FlexNet client row contradicts zero in-use seats")
    return FlexnetStatus(expected_feature, total, observed)


def require_checkout_then_release(
    active_status: str,
    released_status: str,
    expected_feature: str,
    expected_client_host: str,
) -> None:
    """Require the exact client during the Flow and its absence afterward."""
    active = parse_flexnet_lmstat(active_status, expected_feature, expected_client_host)
    if not active.client_observed:
        raise LicenseEvidenceError("FlexNet did not report the Session client checkout")
    released = parse_flexnet_lmstat(released_status, expected_feature, expected_client_host)
    if released.client_observed:
        raise LicenseEvidenceError("FlexNet still reports the Session client after Flow exit")
