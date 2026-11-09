"""CI-script test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def adapter_subprocesses_import_this_checkout() -> None:
    """Override the global fixture: ``src`` metadata would break a fresh env's ``pip check``."""
