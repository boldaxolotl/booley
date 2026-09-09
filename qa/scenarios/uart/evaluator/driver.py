"""Independent clock/MMIO/serial driver; imported only inside the simulator."""

import math
from pathlib import Path

from cocotb.triggers import Timer
from oracles import RATES, check_tx, compare_mmio, serial_bits


class CircuitMismatchError(AssertionError):
    """A contradiction of an explicit circuit contract."""


class ObservationBlockedError(RuntimeError):
    """An observation cannot establish a circuit verdict within its limits."""


class Driver:
    """Drive the interface-only contract and retain every external pin sample."""

    def __init__(self, dut: object, output: Path) -> None:
        self.dut = dut
        self.cycle = 0
        self.last_acceptance = 0
        self.tx: list[int] = []
        self.rx: list[int] = []
        self.irqs: list[int] = []
        self.rx_schedule: dict[int, int] = {}
        self.observations: list[dict] = []
        self.trace = (output / "trace.vcd").open("w", encoding="ascii")
        self.trace.write("$timescale 1ns $end\n$scope module qa_uart $end\n")
        self.signals = [
            ("clk_i", 1),
            ("rst_ni", 1),
            ("req_valid_i", 1),
            ("req_ready_o", 1),
            ("req_write_i", 1),
            ("req_addr_i", 32),
            ("req_wdata_i", 32),
            ("req_wstrb_i", 4),
            ("rsp_valid_o", 1),
            ("rsp_ready_i", 1),
            ("rsp_rdata_o", 32),
            ("rsp_error_o", 1),
            ("rx_i", 1),
            ("tx_o", 1),
            ("irq_o", 9),
        ]
        for index, (name, width) in enumerate(self.signals):
            self.trace.write(f"$var wire {width} s{index} {name} $end\n")
        self.trace.write("$upscope $end\n$enddefinitions $end\n")

    def value(self, name: str) -> int:
        try:
            return int(getattr(self.dut, name).value)
        except ValueError as error:
            raise ObservationBlockedError(
                f"Unresolved external signal {name} at cycle {self.cycle}"
            ) from error

    def expect(self, observed: object, expected: object, reason: str) -> None:
        self.observations.append(
            {"cycle": self.cycle, "reason": reason, "observed": observed, "expected": expected}
        )
        if observed != expected:
            raise CircuitMismatchError(
                f"{reason}: expected {expected}, observed {observed}, cycle {self.cycle}"
            )

    async def tick(self) -> dict:
        if self.cycle >= 1048576:
            raise ObservationBlockedError("Operational source-clock ceiling exhausted")
        if self.cycle in self.rx_schedule:
            self.dut.rx_i.value = self.rx_schedule[self.cycle]
        await Timer(1, unit="ns")
        sampled = {name: self.value(name) for name in ["req_ready_o", "rsp_valid_o"]}
        for name in ["rsp_rdata_o", "rsp_error_o"]:
            sampled[name] = self.value(name) if sampled["rsp_valid_o"] else 0
        self.dut.clk_i.value = 1
        await Timer(9, unit="ns")
        self.trace.write(f"#{self.cycle * 20 + 10}\n")
        for index, (name, _) in enumerate(self.signals):
            self.trace.write(f"b{getattr(self.dut, name).value} s{index}\n")
        self.tx.append(self.value("tx_o"))
        self.rx.append(self.value("rx_i"))
        self.irqs.append(self.value("irq_o"))
        self.dut.clk_i.value = 0
        self.trace.write(f"#{self.cycle * 20 + 20}\n0s0\n")
        await Timer(10, unit="ns")
        self.cycle += 1
        return sampled

    async def wait(self, cycles: int) -> None:
        for _ in range(cycles):
            await self.tick()

    async def reset(self) -> None:
        for name in [
            "clk_i",
            "rst_ni",
            "req_valid_i",
            "req_write_i",
            "req_addr_i",
            "req_wdata_i",
            "req_wstrb_i",
            "rsp_ready_i",
        ]:
            getattr(self.dut, name).value = 0
        self.dut.rx_i.value = 1
        self.rx_schedule.clear()
        # Reset pre-edge outputs may be X; establish the first edge before observing.
        self.dut.clk_i.value = 1
        await Timer(10, unit="ns")
        self.dut.clk_i.value = 0
        await Timer(10, unit="ns")
        await self.wait(4)
        self.dut.rst_ni.value = 1
        await self.wait(2)

    async def transfer(
        self, address: int, write: bool = False, data: int = 0, strobe: int = 15, stall: int = 0
    ) -> tuple[int, int]:
        self.dut.req_addr_i.value = address
        self.dut.req_write_i.value = int(write)
        self.dut.req_wdata_i.value = data
        self.dut.req_wstrb_i.value = strobe
        self.dut.req_valid_i.value = 1
        self.dut.rsp_ready_i.value = 0
        for _ in range(4):
            sampled = await self.tick()
            if sampled["req_ready_o"]:
                self.last_acceptance = self.cycle - 1
                break
        else:
            raise CircuitMismatchError("Idle request acceptance exceeded four clocks")
        self.dut.req_valid_i.value = 0
        for _ in range(4):
            sampled = await self.tick()
            if sampled["rsp_valid_o"]:
                break
        else:
            raise CircuitMismatchError("Response presentation exceeded four clocks")
        for _ in range(stall):
            stalled = await self.tick()
            self.expect(
                [stalled[k] for k in ["rsp_valid_o", "rsp_rdata_o", "rsp_error_o"]],
                [sampled[k] for k in ["rsp_valid_o", "rsp_rdata_o", "rsp_error_o"]],
                "Stable stalled response",
            )
        self.dut.rsp_ready_i.value = 1
        await self.tick()
        self.dut.rsp_ready_i.value = 0
        self.observations.append(
            {
                "cycle": self.cycle,
                "address": address,
                "write": write,
                "data": data,
                "strobe": strobe,
                "response": sampled["rsp_rdata_o"],
                "error": sampled["rsp_error_o"],
            }
        )
        return sampled["rsp_rdata_o"], sampled["rsp_error_o"]

    async def read(
        self,
        address: int,
        expected: int | None = None,
        mask: int = 0xFFFFFFFF,
        strobe: int = 15,
        stall: int = 0,
    ) -> int:
        value, error = await self.transfer(address, strobe=strobe, stall=stall)
        self.expect(error, 0, "Mapped read succeeds")
        if expected is not None:
            self.expect(
                compare_mmio(value, expected, mask),
                True,
                f"MMIO 0x{address:x}: {value:#x} vs {expected:#x}, mask {mask:#x}",
            )
        return value

    async def write(self, address: int, data: int, strobe: int = 15, stall: int = 0) -> None:
        _, error = await self.transfer(address, True, data, strobe, stall)
        self.expect(error, 0, "Mapped write succeeds")

    async def configure(
        self, nco: int = 0x4000, parity: str = "disabled", control: int = 3
    ) -> None:
        bits = (0 if parity == "disabled" else 64) | (128 if parity == "odd" else 0)
        await self.write(0x10, (nco << 16) | bits | control)

    def schedule_rx(
        self,
        payload: list[int],
        nco: int,
        parity: str = "disabled",
        bad_parity: bool = False,
        bad_stop: bool = False,
        gap: int = 0,
    ) -> int:
        start = self.cycle + 1
        bit_number = 0
        for byte in payload:
            bits = serial_bits(byte, parity)
            if bad_parity:
                bits[-2] ^= 1
            if bad_stop:
                bits[-1] = 0
            for bit in bits + [1] * gap:
                self.rx_schedule[start + math.ceil(bit_number * 1048576 / nco)] = bit
                bit_number += 1
        end = start + math.ceil(bit_number * 1048576 / nco)
        self.rx_schedule[end] = 1
        return end

    async def receive(
        self, payload: list[int], nco: int = 0x4000, parity: str = "disabled", gap: int = 0
    ) -> None:
        end = self.schedule_rx(payload, nco, parity, gap=gap)
        await self.wait(end - self.cycle + 2 * max(RATES[nco]))

    async def transmit(
        self, payload: list[int], nco: int = 0x4000, parity: str = "disabled"
    ) -> None:
        await self.configure(nco, parity, control=2)
        for byte in payload:
            await self.write(0x1C, byte)
        start = len(self.tx)
        await self.configure(nco, parity)
        await self.wait((14 * len(payload) + 4) * max(RATES[nco]))
        result = check_tx(self.tx[start:], payload, nco, parity)
        self.expect(result["status"], "pass", result["reason"])
