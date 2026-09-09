"""Simulator entry point; evaluates one frozen operator-owned case."""

import json
import os
from pathlib import Path

import cocotb
from driver import CircuitMismatchError, Driver, ObservationBlockedError
from exercises import EXERCISES


@cocotb.test()
async def observe(dut: object) -> None:
    output = Path(os.environ["QA_CASE_OUTPUT"])
    stimulus = json.loads((output / "stimulus.json").read_text())
    driver = Driver(dut, output)
    status, reason = "blocked", "Evaluator did not finish"
    try:
        await driver.reset()
        await EXERCISES[stimulus["operation"]](driver, stimulus["parameters"])
        status, reason = "pass", "All concrete circuit observations passed"
    except CircuitMismatchError as error:
        status, reason = "fail", str(error)
    except ObservationBlockedError as error:
        status, reason = "blocked", str(error)
    finally:
        driver.trace.close()
        (output / "observations.json").write_text(
            json.dumps(
                {
                    "id": stimulus["id"],
                    "status": status,
                    "reason": reason,
                    "cycles": driver.cycle,
                    "bounds": stimulus["limits"],
                    "observations": driver.observations,
                },
                indent=2,
            )
            + "\n"
        )
