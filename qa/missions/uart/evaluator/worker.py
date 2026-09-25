"""Bounded parent process launches this simulator adapter for build or one case."""

import argparse
from pathlib import Path

from cocotb_tools.runner import get_runner


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--sources", nargs="*", type=Path)
    parser.add_argument("--case", type=Path)
    args = parser.parse_args()
    runner = get_runner("icarus")
    if args.sources:
        runner.build(
            sources=args.sources,
            hdl_toplevel="qa_uart",
            build_dir=args.build,
            always=True,
            build_args=["-g2012"],
            timescale=("1ns", "1ps"),
        )
    else:
        runner.test(
            hdl_toplevel="qa_uart",
            hdl_toplevel_lang="verilog",
            test_module="observe",
            build_dir=args.build,
            test_dir=args.case,
            results_xml=args.case / "transport-results.xml",
            extra_env={
                "QA_CASE_OUTPUT": str(args.case),
                "PYTHONPATH": str(Path(__file__).resolve().parent),
            },
        )


if __name__ == "__main__":
    main()
