"""Approved exact-a, VAL=32 timeout windows observed at external pins and MMIO."""

from driver import CircuitMismatchError, Driver, ObservationBlockedError

BIT_CLOCKS = 64


def event_window(reference: tuple[int, int]) -> tuple[int, int]:
    """Inclusive public window, widened only by measured event uncertainty."""
    return reference[0] + 1920, reference[1] + 2176


async def wait_until(driver: Driver, cycle: int) -> None:
    if driver.cycle > cycle:
        raise ObservationBlockedError("Operator missed the declared timeout intervention")
    await driver.wait(cycle - driver.cycle)


async def timed_event(driver: Driver, reference: tuple[int, int], after: int) -> int:
    """Check the first new IRQ edge, including edges recorded during MMIO work."""
    low, high = event_window(reference)
    position = after + 1
    while position <= high:
        if position >= len(driver.irqs):
            await driver.tick()
            continue
        if driver.irqs[position] & 64 and not driver.irqs[position - 1] & 64:
            driver.observations.append(
                {
                    "timeout_reference": reference,
                    "accepted_window": [low, high],
                    "irq_cycle": position,
                }
            )
            driver.expect(low <= position <= high, True, "Approved 30-34 bit-time timeout window")
            return position
        position += 1
    driver.observations.append(
        {"timeout_reference": reference, "accepted_window": [low, high], "irq_cycle": None}
    )
    raise CircuitMismatchError("No timeout IRQ by approved 34-bit upper bound")


async def received_change(driver: Driver, byte: int, depth: int) -> tuple[int, int]:
    """Bracket a real FIFO-depth change; a full FIFO instead retains its depth."""
    previous = driver.last_acceptance
    start = driver.cycle + 1
    end = driver.schedule_rx([byte], 0x4000)
    bracket = None
    while driver.cycle <= end + 128:
        observed = (await driver.read(0x24) >> 16) & 0xFF
        accepted = driver.last_acceptance
        if accepted - previous > 8:
            raise ObservationBlockedError("FIFO depth observation gap exceeds eight clocks")
        if depth == 64:
            driver.expect(observed, depth, "Dropped byte preserves full FIFO depth")
        elif bracket is None and observed != depth:
            driver.expect(observed, depth + 1, "One received character changes FIFO depth once")
            bracket = (previous, accepted)
        else:
            driver.expect(observed, depth + int(bracket is not None), "Stable observed FIFO depth")
        previous = accepted
    if depth == 64:
        # Counterfactual reset interval from the known external frame plus the
        # approved RX visibility bound, used only to prove windows disjoint.
        bracket = (start + 9 * BIT_CLOCKS, end + 2 * BIT_CLOCKS)
    driver.observations.append(
        {"rx_frame": [start, end], "depth_change_interval": bracket, "dropped": depth == 64}
    )
    driver.expect(bracket is not None, True, "Received character visible within public RX bound")
    assert bracket is not None
    return bracket


async def intervene(driver: Driver, mode: str, first: int, payload: list[int]) -> tuple[int, int]:
    await wait_until(driver, first + 8 * BIT_CLOCKS - 3)
    await driver.read(0x24, len(payload) << 16, 0xFF0000)
    if mode == "read-depth-reset":
        await driver.read(0x18, payload.pop(0), 0xFF)
        reference = (driver.last_acceptance, driver.last_acceptance)
        await driver.read(0x24, len(payload) << 16, 0xFF0000)
    else:
        reference = await received_change(driver, 0xA5, len(payload))
        if mode == "receive-depth-reset":
            payload.append(0xA5)
    if event_window(reference)[0] <= event_window((first, first))[1]:
        raise ObservationBlockedError("Reset and no-reset timeout windows overlap")
    return (first, first) if mode == "full-drop-no-reset" else reference


async def trial(driver: Driver, parameters: dict, changed: bool) -> None:
    mode = parameters["mode"]
    await driver.reset()
    await driver.configure()
    payload = list(parameters["payload"])
    await driver.receive(payload)
    await driver.read(0x24, len(payload) << 16, 0xFF0000)
    await driver.write(4, 64)
    await driver.write(0x30, (1 << 31) | 32)
    enabled = driver.last_acceptance
    first = await timed_event(driver, (enabled, enabled), enabled - 1)
    ack_delay = 10 if changed and mode in ["event-reset", "w1c"] else 2
    await wait_until(driver, first + ack_delay * BIT_CLOCKS)
    await driver.write(0, 64)
    driver.observations.append(
        {
            "timeout_trial": mode,
            "intervene": changed,
            "first_irq": first,
            "w1c_acceptance": driver.last_acceptance,
        }
    )
    await driver.read(0, 0, 64)
    reference = (first, first)
    if changed and mode in ["read-depth-reset", "receive-depth-reset", "full-drop-no-reset"]:
        reference = await intervene(driver, mode, first, payload)
    second = await timed_event(driver, reference, first)
    await driver.write(0, 64)
    await driver.read(0, 0, 64)
    # A further period proves recurrence rather than merely a single lucky edge.
    await timed_event(driver, (second, second), second)
    await driver.write(0x30, 0)
    await driver.write(0, 64)
    for byte in payload:
        await driver.read(0x18, byte, 0xFF)
    await driver.read(0x24, 0, 0xFF0000)
    if mode == "good-recovery":
        await driver.receive([0xA5])
        await driver.read(0x18, 0xA5, 0xFF)
        await driver.read(0x24, 0, 0xFF0000)


async def timeout(driver: Driver, parameters: dict) -> None:
    """Keep the original case IDs, with the approved externally bounded oracle."""
    if parameters["value"] != 32 or parameters["nco"] != 0x4000:
        raise ObservationBlockedError("Approved timeout window requires exact-a and VAL=32")
    if parameters["mode"] == "disabled":
        await driver.configure()
        await driver.write(4, 64)
        await driver.write(0x30, 32)
        start = len(driver.irqs)
        await driver.receive(parameters["payload"])
        await driver.wait(parameters["bit_horizon"] * BIT_CLOCKS)
        driver.expect(
            any(value & 64 for value in driver.irqs[start:]),
            False,
            "Timeout disabled over declared observation horizon",
        )
        return
    for changed in [False, True]:
        await trial(driver, parameters, changed)
