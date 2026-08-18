"""Paid-feature proof trusts fixed vendor status, never Project output."""

import pytest
from tests.license_evidence import (
    LicenseEvidenceError,
    parse_flexnet_lmstat,
    require_checkout_then_release,
)

_ACTIVE = """License server status: 2100@license-server
Users of Implementation:  (Total of 4 licenses issued;  Total of 1 license in use)

  "Implementation" v2025.1201, vendor: xilinxd, expiry: permanent
  agent session-a display (v2025.1201) (license-server/2100 101), start Fri 8/14 10:00
"""

_RELEASED = """License server status: 2100@license-server
Users of Implementation:  (Total of 4 licenses issued;  Total of 0 licenses in use)

  "Implementation" v2025.1201, vendor: xilinxd, expiry: permanent
"""


def test_exact_vendor_checkout_and_release_are_accepted() -> None:
    status = parse_flexnet_lmstat(_ACTIVE, "Implementation", "session-a")
    assert status.total_in_use == 1
    assert status.client_observed is True
    require_checkout_then_release(_ACTIVE, _RELEASED, "Implementation", "session-a")


@pytest.mark.parametrize(
    "text",
    [
        "PROJECT_ECHO Got license for feature Implementation\n",
        "Got license for feature Implementation\nReleasing license for feature Implementation\n",
        "Users of Synthesis: (Total of 4 licenses issued; Total of 1 license in use)\n",
        (
            "Users of Implementation: (Total of 4 licenses issued; Total of 1 license in use)\n"
            '  "Implementation" v2025.1201, vendor: xilinxd\n'
            "  agent another-host display (v2025.1201)\n"
        ),
    ],
)
def test_project_echo_or_wrong_vendor_state_cannot_prove_checkout(text: str) -> None:
    if text.startswith("Users of Implementation"):
        status = parse_flexnet_lmstat(text, "Implementation", "session-a")
        assert status.client_observed is False
    else:
        with pytest.raises(LicenseEvidenceError):
            parse_flexnet_lmstat(text, "Implementation", "session-a")


def test_client_row_with_zero_in_use_is_rejected() -> None:
    contradictory = _ACTIVE.replace("1 license in use", "0 licenses in use")
    with pytest.raises(LicenseEvidenceError, match="contradicts"):
        parse_flexnet_lmstat(contradictory, "Implementation", "session-a")


def test_release_must_remove_exact_session_client() -> None:
    with pytest.raises(LicenseEvidenceError, match="still reports"):
        require_checkout_then_release(_ACTIVE, _ACTIVE, "Implementation", "session-a")
