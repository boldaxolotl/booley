"""Multi-file TB helper — proves package-dir imports under copyto (spike S1)."""


def expected_after(start: int, cycles: int, width: int = 8) -> int:
    return (start + cycles) % (1 << width)
