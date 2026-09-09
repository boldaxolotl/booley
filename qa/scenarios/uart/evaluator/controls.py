"""Run positive, one-bit corruption and restored controls through the real adapter."""

import argparse
import json
import time
from pathlib import Path

from cases import case, evaluator_identity
from run import HERE, case_result, launch


def controls(output: Path) -> dict:
    """Qualify the transport path, without claiming candidate UART coverage."""
    output.mkdir(parents=True, exist_ok=False)
    source = (HERE / "controls/transport.sv").read_text()
    expected = {"positive": "pass", "corrupt": "fail", "restored": "pass"}
    results = {}
    deadline = time.monotonic() + 1800
    for family in ["mmio", "serial"]:
        stimulus = (
            case(
                "UART-REG.CTRL.control",
                "register",
                address=0x10,
                mode="reset-defined",
                expected=0,
                mask=0xFFFF03F7,
            )
            if family == "mmio"
            else case("UART-TXRX.control", "tx", payload=[0x55], nco=0x4000, parity="disabled")
        )
        for variant, verdict in expected.items():
            directory = output / f"{family}-{variant}"
            directory.mkdir()
            content = source.replace(
                f"CORRUPT_{family.upper()} = 0",
                f"CORRUPT_{family.upper()} = {int(variant == 'corrupt')}",
            )
            fixture = directory / "transport.sv"
            fixture.write_text(content)
            build = directory / "build"
            code = launch(
                ["--build", str(build), "--sources", str(fixture)],
                directory / "build.log",
                min(300, deadline - time.monotonic()),
            )
            if code:
                raise RuntimeError(f"Control fixture build failed: {directory}")
            result = case_result(stimulus, build, directory, deadline)
            results[f"{family}-{variant}"] = result
            if result["status"] != verdict:
                raise RuntimeError(f"Control {family}/{variant} expected {verdict}: {result}")
    (output / "controls.json").write_text(
        json.dumps({"evaluator_sha256": evaluator_identity(), "results": results}, indent=2) + "\n"
    )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    controls(args.output.resolve())


if __name__ == "__main__":
    main()
