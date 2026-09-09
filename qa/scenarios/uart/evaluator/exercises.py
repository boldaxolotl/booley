"""Public-contract UART stimuli, independent of candidate RTL and testbench."""

from cases import REGISTERS
from driver import Driver, ObservationBlockedError
from oracles import check_tx, check_val


async def register(driver: Driver, parameters: dict) -> None:
    address, mode = parameters["address"], parameters["mode"]
    if mode == "reset-defined":
        await driver.read(address, parameters["expected"], parameters["mask"])
    elif mode == "reserved-write":
        await driver.write(address, ~parameters["mask"] & 0xFFFFFFFF)
        await driver.read(address, 0, ~parameters["mask"] & 0xFFFFFFFF)
    else:
        await driver.read(address, 0, ~parameters["mask"] & 0xFFFFFFFF)


async def field(driver: Driver, parameters: dict) -> None:
    address = REGISTERS[parameters["register"]][0]
    shift, width = parameters["shift"], parameters["width"]
    mask = ((1 << width) - 1) << shift
    access = parameters["access"]
    if access == "rw":
        before = await driver.read(address)
        expected = (before & ~mask) | (parameters["value"] << shift)
        await driver.write(address, expected)
        await driver.read(address, expected, REGISTERS[parameters["register"]][1])
    elif access == "wo":
        await driver.read(address, 0, mask)
    elif access == "rw1c":
        await event_field(driver, parameters, mask)
    else:
        await readonly_field(driver, parameters, mask)


async def readonly_field(driver: Driver, parameters: dict, mask: int) -> None:
    name = parameters["register"]
    address = REGISTERS[name][0]
    if name == "RDATA":
        await driver.configure()
        await driver.receive([0x55, 0xAA])
        await driver.write(address, 0xFFFFFFFF)
        await driver.read(0x24, 2 << 16, 0xFF0000)
        await driver.read(address, 0x55, 0xFF)
        await driver.read(address, 0xAA, 0xFF)
        await driver.read(0x24, 0, 0xFF0000)
    elif name == "VAL":
        await history(driver, {"forbidden_write": True})
    else:
        before = await driver.read(address)
        await driver.write(address, 0xFFFFFFFF)
        after = await driver.read(address)
        driver.expect(after & mask, before & mask, "RO writes have no effect")


async def event_field(driver: Driver, parameters: dict, mask: int) -> None:
    await driver.write(0x08, mask)
    await driver.read(0x00, mask, mask)
    mode = parameters["mode"]
    data = 0 if mode == "w1c-zero" else mask
    strobe = 0 if mode == "w1c-masked" else 15
    if mode == "w1c-isolation":
        other = 1 << (2 if parameters["shift"] != 2 else 3)
        await driver.write(0x08, other)
        await driver.write(0, mask)
        await driver.read(0, other, mask | other)
    else:
        await driver.write(0, data, strobe)
        await driver.read(0, 0 if data and strobe else mask, mask)


async def invalid(driver: Driver, parameters: dict) -> None:
    await driver.configure(control=2)
    await driver.receive([0x55, 0xAA])
    await driver.write(0x1C, 0x33)
    await driver.write(0x20, (2 << 5) | (2 << 2))
    await driver.write(0x28, 2)
    await driver.write(0x30, 0x55AA)
    await driver.write(4, 0xFF)
    await driver.write(8, 0x54)
    await driver.wait(128)
    # Every non-destructive CSR is stable in this epoch: TX disabled, RX idle,
    # timeout disabled, no loopback, and VAL settled to the constant input.
    before = {
        name: await driver.read(fields[0]) for name, fields in REGISTERS.items() if name != "RDATA"
    }
    value, error = await driver.transfer(parameters["address"], parameters["write"], 0xFFFFFFFF)
    driver.expect((value, error), (0, 1), "Invalid full-width byte address returns error/zero")
    for name, value in before.items():
        await driver.read(REGISTERS[name][0], value, REGISTERS[name][1])
    await driver.read(0x18, 0x55, 0xFF)
    await driver.read(0x18, 0xAA, 0xFF)
    await driver.read(0x24, 0, 0xFF0000)


async def bus(driver: Driver, parameters: dict) -> None:
    mode, strobe = parameters["mode"], parameters.get("strobe", 15)
    if mode == "byte-masks":
        await driver.write(0x30, 0x8055AA55, strobe)
        lanes = sum(0xFF << (8 * bit) for bit in range(4) if strobe & (1 << bit))
        await driver.read(0x30, 0x8055AA55 & lanes)
    elif mode == "w1c-masks":
        await driver.write(8, 0xFC)
        await driver.write(0, 0xFC, strobe)
        await driver.read(0, 0 if strobe & 1 else 0xFC, 0xFC)
    elif mode in ["wdata-strobe", "write-once"]:
        await driver.write(0x1C, 0x55, strobe, stall=8)
        await driver.read(0x24, int(bool(strobe & 1)), 0xFF)
    elif mode in ["read-once", "read-snapshot"]:
        await driver.configure()
        await driver.receive([0x55, 0xAA])
        await driver.read(0x18, 0x55, 0xFF, stall=8)
        await driver.read(0x24, 1 << 16, 0xFF0000)
        await driver.read(0x18, 0xAA, 0xFF)
    elif mode == "empty-read":
        for _ in range(4):
            await driver.read(0x18)
        await driver.read(0x24, 0, 0xFF0000)
    elif mode == "read-strobes":
        await driver.write(0x10, 0x40000003)
        await driver.read(0x10, 0x40000003, strobe=strobe)
    elif mode == "single-outstanding":
        await single_outstanding(driver)
    else:
        await driver.write(0x10, 0x40000003, stall=8)
        await driver.read(0x10, 0x40000003, stall=8)


async def single_outstanding(driver: Driver) -> None:
    driver.dut.req_valid_i.value = 1
    driver.dut.req_addr_i.value = 0x10
    driver.dut.rsp_ready_i.value = 0
    accepted = 0
    for _ in range(12):
        sample = await driver.tick()
        accepted += sample["req_ready_o"]
    driver.expect(accepted, 1, "Only one request accepted while response remains pending")
    driver.dut.req_valid_i.value = 0
    driver.dut.rsp_ready_i.value = 1
    await driver.wait(2)


async def tx(driver: Driver, parameters: dict) -> None:
    await driver.transmit(
        parameters["payload"], parameters["nco"], parameters.get("parity", "disabled")
    )


async def rx(driver: Driver, parameters: dict) -> None:
    await driver.configure(parameters["nco"], parameters.get("parity", "disabled"))
    await driver.receive(
        parameters["payload"],
        parameters["nco"],
        parameters.get("parity", "disabled"),
        parameters.get("gap", 0),
    )
    for byte in parameters["payload"]:
        await driver.read(0x18, byte, 0xFF)
    await driver.read(0x24, 0, 0xFF0000)


async def parity(driver: Driver, parameters: dict) -> None:
    await tx(driver, parameters)
    await rx(driver, parameters)


async def baud(driver: Driver, parameters: dict) -> None:
    if parameters["nco"]:
        await tx(driver, parameters)
        await rx(driver, parameters)
    else:
        await driver.configure(0)
        await driver.write(0x1C, 0x55)
        start = len(driver.tx)
        await driver.wait(4096)
        driver.expect(set(driver.tx[start:]), {1}, "NCO zero: no TX progress during 4096 clocks")


async def traffic(driver: Driver, parameters: dict) -> None:
    mode = parameters["mode"]
    if mode == "enable-disable":
        await driver.configure(control=0)
        await driver.write(0x1C, 0x55)
        await driver.receive([0xAA])
        await driver.read(0x24, 1, 0x00FF00FF)
        await driver.configure()
        await driver.wait(14 * 64)
        await rx(driver, parameters)
    elif mode == "full-duplex":
        await driver.configure()
        driver.schedule_rx(parameters["payload"], parameters["nco"])
        await tx(driver, parameters)
        for byte in parameters["payload"]:
            await driver.read(0x18, byte, 0xFF)
    else:
        await parity(driver, parameters)


async def fifo(driver: Driver, parameters: dict) -> None:
    direction, payload = parameters["direction"], parameters["payload"]
    await driver.configure(control=2)
    if direction == "tx":
        for byte in payload:
            await driver.write(0x1C, byte)
        await driver.read(0x24, parameters["depth"], 0xFF)
        await driver.read(0x14, (int(len(payload) == 32)) | (int(not payload) << 2), 5)
    else:
        if payload:
            await driver.receive(payload)
        await driver.read(0x24, parameters["depth"] << 16, 0xFF0000)
        await driver.read(0x14, (int(len(payload) == 64) << 1) | (int(not payload) << 5), 0x22)
    if parameters.get("drain", True):
        await drain(driver, direction, payload)


async def drain(driver: Driver, direction: str, payload: list[int]) -> None:
    if direction == "rx":
        for byte in payload:
            await driver.read(0x18, byte, 0xFF)
        await driver.read(0x24, 0, 0xFF0000)
    elif payload:
        start = len(driver.tx)
        await driver.configure()
        await driver.wait((14 * len(payload) + 4) * 64)
        result = check_tx(driver.tx[start:], payload, 0x4000)
        driver.expect(
            result["status"], "pass", "TX FIFO ordered complete drain: " + result["reason"]
        )
        await driver.read(0x24, 0, 0xFF)


async def watermark(driver: Driver, parameters: dict) -> None:
    direction = parameters["direction"]
    encoding = parameters["encoding"] << (5 if direction == "tx" else 2)
    await driver.write(0x20, encoding)
    await fifo(driver, {**parameters, "drain": False})
    bit = 0 if direction == "tx" else 1
    await driver.read(0, int(parameters["expected"]) << bit, 1 << bit)
    await drain(driver, direction, parameters["payload"])


async def fifo_behavior(driver: Driver, parameters: dict) -> None:
    mode = parameters["mode"]
    if mode == "overflow":
        await driver.configure()
        await driver.receive(parameters["payload"])
        await driver.receive([0xFE])
        await driver.read(0, 8, 8)
        await driver.read(0x24, 64 << 16, 0xFF0000)
        await drain(driver, "rx", parameters["payload"])
        await driver.write(0, 8)
        await rx(driver, {"payload": [0xA5], "nco": 0x4000})
    elif mode == "order":
        await fifo(driver, {"direction": "tx", "payload": parameters["payload"][:32], "depth": 32})
        await fifo(driver, {"direction": "rx", "payload": parameters["payload"], "depth": 64})
    else:
        await driver.configure(control=2)
        await driver.write(0x1C, 0x55)
        await driver.receive([0xAA])
        await driver.write(0x20, 2 if mode == "tx-reset" else 1)
        await driver.read(0x24, 0x10000 if mode == "tx-reset" else 1, 0x00FF00FF)
        await driver.reset()
        await parity(driver, {"payload": [0xA5], "nco": 0x4000, "parity": "disabled"})


async def irq(driver: Driver, parameters: dict) -> None:
    bit, mode = parameters["bit"], parameters["mode"]
    mask = 1 << bit
    await driver.configure(control=2)
    await driver.write(4, 0 if mode in ["mask", "enable"] else mask)
    if mode == "inject":
        await injected_irq(driver, bit)
        return
    start = len(driver.irqs)
    await natural_irq(driver, bit)
    await driver.read(0, mask, mask)
    driver.expect(
        any(value & ~mask for value in driver.irqs[start:]),
        False,
        "No cross-bit enabled IRQ output",
    )
    driver.expect(
        any(value & mask for value in driver.irqs[start:]),
        mode == "identity",
        "Natural source respects enable mask",
    )
    if mode == "enable":
        await driver.write(4, mask)
        await driver.wait(64)
        driver.expect(driver.irqs[-1] & mask, mask, "Enable exposes existing source state")
        await driver.write(4, 0)
        await driver.wait(64)
        driver.expect(driver.irqs[-1] & mask, 0, "Disable masks without clearing source")
        await driver.read(0, mask, mask)
    if bit not in [0, 1, 8]:
        await driver.write(0, mask)
        await driver.read(0, 0, mask)


async def natural_irq(driver: Driver, bit: int) -> None:
    if bit in [0, 8]:
        await driver.write(0x1C, 0x55)
        await driver.configure()
        await driver.wait(14 * 64)
    elif bit == 1:
        await driver.receive([0x55])
    elif bit == 2:
        await driver.transmit([0x55])
    elif bit == 3:
        await driver.receive([*range(64), 0xFE])
    elif bit in [4, 7]:
        parity_mode = "even" if bit == 7 else "disabled"
        await driver.configure(parity=parity_mode)
        end = driver.schedule_rx([0], 0x4000, parity_mode, bad_parity=bit == 7, bad_stop=bit == 4)
        await driver.wait(end - driver.cycle + 128)
    elif bit == 5:
        driver.dut.rx_i.value = 0
        await driver.wait(22 * 64)
        driver.dut.rx_i.value = 1
        await driver.wait(64)
    else:
        await driver.write(0x30, (1 << 31) | 32)
        await driver.receive([0x55])
        await await_timeout(driver, 4096 * 64)


async def injected_irq(driver: Driver, bit: int) -> None:
    mask = 1 << bit
    states = [False, True] if bit in [0, 1, 8] else [False]
    for active in states:
        if bit in [0, 8]:
            await driver.write(0x20, 2)
            if not active:
                await driver.write(0x1C, 0x55)
        elif bit == 1:
            await driver.write(0x20, 1)
            if active:
                await driver.receive([0x55])
        start = len(driver.irqs)
        await driver.write(8, mask)
        await driver.wait(64)
        driver.expect(
            any(value & mask for value in driver.irqs[start:]),
            True,
            "Injection visible to independent per-clock monitor",
        )
        driver.expect(
            any(value & ~mask for value in driver.irqs[start:]),
            False,
            "Injection has no cross-bit enabled effect",
        )
        await driver.read(0, mask if active or bit not in [0, 1, 8] else 0, mask)
        if bit in [0, 1, 8]:
            await driver.write(0, mask)
            await driver.read(0, mask if active else 0, mask)
        else:
            await driver.write(0, mask)
            await driver.read(0, 0, mask)


async def await_timeout(driver: Driver, horizon: int) -> int:
    """Observe an event; exhaustion is not a newly invented circuit deadline."""
    for _ in range(horizon):
        if await driver.read(0) & 64:
            return driver.last_acceptance
    raise ObservationBlockedError("No timeout event within operational observation horizon")


async def error(driver: Driver, parameters: dict) -> None:
    bad_parity = parameters.get("parity") in ["even", "odd"]
    parity_mode = parameters.get("parity", "disabled")
    await driver.configure(parity=parity_mode)
    end = driver.schedule_rx(
        [0], 0x4000, parity_mode, bad_parity=bad_parity, bad_stop=not bad_parity
    )
    await driver.wait(end - driver.cycle + 128)
    mask = 128 if bad_parity else 16
    await driver.read(0, mask, mask)
    await driver.read(0x24, 0, 0xFF0000)
    await driver.write(0, mask)
    await rx(driver, {"payload": parameters["payload"], "nco": 0x4000, "parity": parity_mode})


async def break_error(driver: Driver, parameters: dict) -> None:
    parity_mode = parameters["parity"]
    await driver.configure(parity=parity_mode, control=3 | parameters["encoding"] << 8)
    threshold = parameters["characters"] * (11 if parity_mode != "disabled" else 10) * 64
    driver.dut.rx_i.value = 0
    await driver.wait(max(1, threshold - 128))
    await driver.read(0, 0, 32)
    await driver.wait(384)
    await driver.read(0, 32, 32)
    await driver.write(0, 32)
    await driver.wait(threshold + 128)
    await driver.read(0, 0, 32)
    driver.dut.rx_i.value = 1
    await driver.wait(64)
    driver.dut.rx_i.value = 0
    await driver.wait(threshold + 256)
    await driver.read(0, 32, 32)
    await driver.write(0, 32)
    driver.dut.rx_i.value = 1
    await driver.wait(128)
    await rx(driver, {"payload": parameters["payload"], "nco": 0x4000, "parity": parity_mode})


async def timeout(driver: Driver, parameters: dict) -> None:
    mode = parameters["mode"]
    await driver.configure()
    await driver.write(4, 64)
    await driver.write(0x30, parameters["value"] | (0 if mode == "disabled" else 1 << 31))
    await driver.receive(parameters["payload"])
    if mode == "disabled":
        start = len(driver.irqs)
        await driver.wait(parameters["bit_horizon"] * 64)
        driver.expect(
            any(value & 64 for value in driver.irqs[start:]),
            False,
            "Timeout disabled over declared observation horizon",
        )
        return
    await await_timeout(driver, parameters["bit_horizon"] * 64)
    await driver.read(0, 64, 64)
    if mode in ["enabled", "w1c", "good-recovery"]:
        await driver.write(0, 64)
        await driver.read(0, 0, 64)
        if mode == "good-recovery":
            await driver.write(0x30, 0)
            await drain(driver, "rx", parameters["payload"])
            await rx(driver, {"payload": [0xA5], "nco": 0x4000})
        return
    observations = []
    for intervene in [False, True]:
        observations.append(await timeout_trial(driver, parameters, intervene))
    driver.observations.append({"timeout_pair": observations, "mode": mode})
    raise ObservationBlockedError(
        "Paired timeout stimuli retained; public phase variation has no universal "
        "comparison tolerance, so these timestamps alone cannot establish conformance"
    )


async def timeout_trial(driver: Driver, parameters: dict, intervene: bool) -> dict:
    await driver.reset()
    await driver.configure()
    mode = parameters["mode"]
    payload = list(range(64)) if mode == "full-drop-no-reset" else [0x55, 0xAA]
    await driver.receive(payload)
    await driver.write(0x30, (1 << 31) | parameters["value"])
    first = await await_timeout(driver, parameters["bit_horizon"] * 64)
    await driver.write(0, 64)
    await driver.wait(8 * 64)
    before = await driver.read(0x24)
    intervention = driver.cycle
    if intervene and mode == "read-depth-reset":
        await driver.read(0x18, payload[0], 0xFF)
    elif intervene and mode in ["receive-depth-reset", "full-drop-no-reset"]:
        await driver.receive([0xA5])
    elif intervene and mode == "event-reset":
        # W1C timing must not substitute for the event that reset the counter.
        await driver.write(0, 64)
    else:
        await driver.wait(12 * 64)
    after = await driver.read(0x24)
    second = await await_timeout(driver, parameters["bit_horizon"] * 64)
    if intervene and mode == "full-drop-no-reset":
        driver.expect(after & 0xFF0000, before & 0xFF0000, "Dropped byte preserves full depth")
        await drain(driver, "rx", payload)
    return {
        "intervene": intervene,
        "first_event_read": first,
        "intervention": intervention,
        "second_event_read": second,
        "before_depth": before,
        "after_depth": after,
    }


async def noise_filter(driver: Driver, parameters: dict) -> None:
    await driver.configure(control=3 | (parameters["nf"] << 2))
    await driver.wait(parameters["phase_quarters"])
    if parameters["mode"] == "phase-start":
        await driver.receive(parameters["payload"])
        for byte in parameters["payload"]:
            await driver.read(0x18, byte, 0xFF)
    else:
        driver.dut.rx_i.value = 0
        await driver.wait(1 if parameters["mode"] == "noise" else 16)
        driver.dut.rx_i.value = 1
        await driver.wait(14 * 64)
        await driver.read(0x24, 0, 0xFF0000)
        await driver.receive(parameters["payload"])
        for byte in parameters["payload"]:
            await driver.read(0x18, byte, 0xFF)


async def loop(driver: Driver, parameters: dict) -> None:
    if parameters["mode"] == "system":
        await driver.configure(control=0x13)
        start = len(driver.tx)
        for byte in parameters["payload"]:
            await driver.write(0x1C, byte)
        await driver.wait((len(parameters["payload"]) * 14 + 2) * 64)
        driver.expect(set(driver.tx[start:]), {1}, "System loopback keeps external TX idle")
        for byte in parameters["payload"]:
            await driver.read(0x18, byte, 0xFF)
    else:
        await driver.configure(control=0x23)
        start = len(driver.tx)
        await driver.receive(parameters["payload"])
        result = check_tx(driver.tx[start:], parameters["payload"], 0x4000)
        driver.expect(result["status"], "pass", "Line loopback forwards serial RX")
        await driver.read(0x24, 0, 0xFF0000)
    await driver.configure()
    await parity(driver, {**parameters, "parity": "disabled"})


async def override(driver: Driver, parameters: dict) -> None:
    await driver.configure(control=2)
    for byte in parameters["payload"]:
        await driver.write(0x1C, byte)
    level = int(parameters["mode"] == "high")
    await driver.write(0x28, 1 | (level << 1))
    await driver.wait(64)
    driver.expect(driver.tx[-1], level, "TX override pin level")
    await driver.read(0x24, len(parameters["payload"]), 0xFF)
    await driver.write(0x28, 0)
    await drain(driver, "tx", parameters["payload"])


async def history(driver: Driver, parameters: dict) -> None:
    await driver.configure(control=2)
    start = len(driver.rx)
    pattern = parameters.get("pattern", [0, 1, 1, 0, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 1, 0])
    reads = []
    complete = pattern + [value ^ 1 for value in pattern] + pattern
    for index, bit in enumerate(complete):
        driver.rx_schedule[driver.cycle + index * 4] = bit
    end = driver.cycle + len(complete) * 4
    while driver.cycle < end:
        if parameters.get("forbidden_write"):
            await driver.write(0x2C, 0xFFFFFFFF)
        value = await driver.read(0x2C)
        reads.append((driver.last_acceptance - start, value))
    settled = [(cycle, value) for cycle, value in reads if cycle >= 80]
    driver.expect(
        check_val(driver.rx[start:], settled, 0x4000),
        True,
        "One consistent VAL phase/delay and newest-bit-zero ordering",
    )


async def reset_case(driver: Driver, parameters: dict) -> None:
    await driver.configure()
    if parameters["mode"] in ["active-tx", "occupied"]:
        await driver.write(0x1C, 0x55)
    if parameters["mode"] == "active-rx":
        driver.schedule_rx([0x55], 0x4000)
        await driver.wait(128)
    if parameters["mode"] == "occupied":
        await driver.receive([0xAA])
    if parameters["mode"] == "pending-mmio":
        driver.dut.req_valid_i.value = 1
        driver.dut.req_addr_i.value = 0x10
        driver.dut.rsp_ready_i.value = 0
        await driver.wait(4)
    await driver.reset()
    await driver.read(0x10, 0)
    await driver.read(0x24, 0, 0x00FF00FF)
    await parity(driver, {**parameters, "parity": "disabled"})


EXERCISES = {
    "register": register,
    "field": field,
    "invalid": invalid,
    "bus": bus,
    "tx": tx,
    "rx": rx,
    "parity": parity,
    "baud": baud,
    "traffic": traffic,
    "fifo": fifo,
    "watermark": watermark,
    "fifo-behavior": fifo_behavior,
    "irq": irq,
    "error": error,
    "parity-error": error,
    "break": break_error,
    "timeout": timeout,
    "filter": noise_filter,
    "loop": loop,
    "override": override,
    "history": history,
    "reset": reset_case,
}
