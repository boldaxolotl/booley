"""Fixed operator controls and their independent, single-defect hardware variants."""

from cases import case

VARIANTS = {"positive": "pass", "corrupt": "fail", "restored": "pass"}


def receiver(name: str, mutation: str, family: str, action: str, **parameters: object) -> tuple:
    """Use the independent receiver hardware for one public-contract exercise."""
    return name, "receiver", mutation, case(f"{family}.control.{name}", action, **parameters)


def receiver_cases() -> list[tuple]:
    result = []
    for nf in [0, 1]:
        for phase in range(4):
            result.append(receiver(
                f"rx-nf{nf}-phase{phase}", "rx", "UART-FILTER", "filter",
                mode="phase-start", nf=nf, phase_quarters=phase, payload=[0x55, 0xA6]))
    for parity in ["even", "odd"]:
        result.append(receiver(
            f"rx-{parity}", "rx", "UART-PARITY", "rx", payload=[0x55, 0xA7],
            nco=0x4000, parity=parity))
    result.extend([
        receiver("fifo", "depth", "UART-FIFO", "fifo", direction="rx", depth=3,
                 payload=[0x55, 0xAA, 0x37]),
        receiver("watermark", "watermark", "UART-WATERMARK", "watermark", direction="rx",
                 encoding=1, depth=2, expected=True, payload=[0x55, 0xAA]),
        receiver("irq-natural", "irq", "UART-IRQ", "irq", bit=1, mode="identity"),
        receiver("irq-injected", "irq", "UART-IRQ", "irq", bit=7, mode="inject"),
        receiver("history", "history", "UART-HISTORY", "history"),
        receiver("read-once", "rx", "UART-BUS", "bus", mode="read-once"),
    ])
    return result


def control_cases() -> list[tuple]:
    """Return name, owned source, mutation and scored stimulus for each control."""
    return [
        ("mmio", "transport", "mmio", case("UART-REG.CTRL.control", "register",
            address=0x10, mode="reset-defined", expected=0, mask=0xFFFF03F7)),
        ("serial", "transport", "serial", case("UART-TXRX.control", "tx",
            payload=[0x55], nco=0x4000, parity="disabled")),
        *receiver_cases(),
    ]


def expected_controls() -> dict[str, str]:
    """Require every good/independently corrupted/restored observation path."""
    return {f"{name}-{variant}": status for name, *_ in control_cases()
            for variant, status in VARIANTS.items()}
