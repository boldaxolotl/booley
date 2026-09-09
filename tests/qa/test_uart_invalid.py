"""Controls for rejected MMIO accesses preserving observable occupied state."""

import asyncio
import importlib
from pathlib import Path

import pytest


class RegisterTransport:
    """Minimal bus control with independently selectable destructive decoder faults."""

    def __init__(self, fault):
        self.fault = fault
        self.registers = {0x10: 0}
        self.fifo = []

    async def configure(self, **kwargs):
        self.registers[0x10] = 0x40000002

    async def receive(self, payload):
        self.fifo.extend(payload)

    async def wait(self, cycles):
        pass

    async def write(self, address, value):
        self.registers[address] = value

    async def read(self, address, expected=None, mask=0xFFFFFFFF):
        if address == 0x18:
            value = self.fifo.pop(0) if self.fifo else 0
        elif address == 0x24:
            value = len(self.fifo) << 16
        else:
            value = self.registers.get(address, 0)
        if expected is not None:
            self.expect(value & mask, expected & mask, "MMIO read")
        return value

    async def transfer(self, address, write, data):
        if self.fault == "pop" and self.fifo:
            self.fifo.pop(0)
        if self.fault == "append":
            self.fifo.append(0xFF)
        if self.fault == "clear-control":
            self.registers[0x10] = 0
        return 0, 1

    def expect(self, value, expected, reason):
        assert value == expected, reason


@pytest.mark.parametrize("fault", ["pop", "clear-control", "append"])
@pytest.mark.parametrize("write", [False, True])
def test_invalid_access_detects_state_loss_and_restoration(monkeypatch, fault, write):
    evaluator = Path(__file__).resolve().parents[2] / "qa/scenarios/uart/evaluator"
    monkeypatch.syspath_prepend(str(evaluator))
    exercise = importlib.import_module("exercises").invalid
    parameters = {"address": 0x10018, "write": write}
    asyncio.run(exercise(RegisterTransport(None), parameters))
    with pytest.raises(AssertionError):
        asyncio.run(exercise(RegisterTransport(fault), parameters))
    asyncio.run(exercise(RegisterTransport(None), parameters))
