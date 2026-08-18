"""Keep fixture *projects* out of pytest collection.

Files under tests/fixtures/ are e2e fixture content, not host tests — the
cocotb fixture's ``test_counter.py`` matches pytest's ``test_*.py`` glob but
imports ``cocotb``, which exists only in the sandbox image.
"""

collect_ignore_glob = ["*"]
