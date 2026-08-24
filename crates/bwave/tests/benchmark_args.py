"""Shared argparse boundary adapters for B-Wave benchmark tooling."""

from __future__ import annotations

import argparse

from booley.core.boundary import as_float, as_int, as_positive_int


def _integer(text: str) -> int:
    normalized = text.strip()
    digits = normalized[1:] if normalized[:1] in {"+", "-"} else normalized
    value = as_int(text)
    if not digits.isdigit() or value is None:
        raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}")
    return value


def positive_int(text: str) -> int:
    """Parse a strictly positive integer CLI argument."""
    value = _integer(text)
    if as_positive_int(value, 0) == 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return value


def nonnegative_int(text: str) -> int:
    """Parse a non-negative integer CLI argument."""
    value = _integer(text)
    if value < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return value


def positive_float(text: str) -> float:
    """Parse a finite, strictly positive floating-point CLI argument."""
    value = as_float(text)
    if value is None:
        raise argparse.ArgumentTypeError(f"expected a number, got {text!r}")
    if value <= 0:
        raise argparse.ArgumentTypeError("value must be finite and positive")
    return value
