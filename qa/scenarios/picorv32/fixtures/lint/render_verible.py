"""Render the byte-exact trailing-space input in a disposable run-owned path."""

import argparse
from pathlib import Path


def render(destination: Path) -> None:
    """Produce one no-trailing-spaces warning without a module-filename warning."""
    destination.write_bytes(b"module qa_lint_fixture;\n  logic clk;  \nendmodule\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    render(args.destination)
