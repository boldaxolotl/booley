"""Freeze concrete public UART cases and counter-based private supplements."""

import argparse
import copy
import hashlib
import json
import re
import struct
from pathlib import Path

PREFIX = "opentitan-uart-clean-room-greenfield."
SOURCE = "https://github.com/lowRISC/opentitan/blob/615d3c74fadbbf674c8ca05a70f91094989849fb/hw/ip/uart/doc/"
FAMILIES = (
    "UART-TXRX",
    "UART-BAUD",
    "UART-PARITY",
    "UART-FIFO",
    "UART-WATERMARK",
    "UART-IRQ",
    "UART-ERROR",
    "UART-FILTER",
    "UART-LOOP",
    "UART-OVERRIDE",
    "UART-HISTORY",
    "UART-RESET",
)
# Offsets, defined masks and scored reset masks come from the reviewed field table.
REGISTERS = {
    "INTR_STATE": (0x00, 0x1FF, 0x101, 0x1FF),
    "INTR_ENABLE": (0x04, 0x1FF, 0, 0x1FF),
    "INTR_TEST": (0x08, 0x1FF, 0, 0x1FF),
    "ALERT_TEST": (0x0C, 1, 0, 1),
    "CTRL": (0x10, 0xFFFF03F7, 0, 0xFFFF03F7),
    "STATUS": (0x14, 0x3F, 0x3C, 0x3C),
    "RDATA": (0x18, 0xFF, 0, 0),
    "WDATA": (0x1C, 0xFF, 0, 0xFF),
    "FIFO_CTRL": (0x20, 0xFF, 0, 0xFF),
    "FIFO_STATUS": (0x24, 0x00FF00FF, 0, 0),
    "OVRD": (0x28, 3, 0, 3),
    "VAL": (0x2C, 0xFFFF, 0, 0),
    "TIMEOUT_CTRL": (0x30, 0x80FFFFFF, 0, 0x80FFFFFF),
}


def case(identity: str, operation: str, **parameters: object) -> dict:
    full = PREFIX + identity
    return {
        "id": full,
        "operation": operation,
        "parameters": parameters,
        "source": SOURCE
        + ("registers.md" if identity.startswith("UART-REG") else "theory_of_operation.md"),
        "addenda": ["spec/mmio-addendum.md", "spec/timing-addendum.md"],
        "setup": "Reset for at least four rising edges with RX high; release on clock boundary.",
        "recovery": "Reset/reestablish the declared state; error/FIFO/reset families also prove good communication.",
        "limits": {"wall_seconds": 30, "cycles": 1048576},
        "evidence": {
            name: f"evidence/uart/evaluator/<evaluation>/cases/{full}/{name}"
            for name in ["stimulus.json", "observations.json", "trace.vcd", "execution.log"]
        },
    }


def register_fields() -> list[dict]:
    """Read only the frozen documentation's wavejson field descriptions."""
    path = Path(__file__).parent.parent / "spec/corpus/hw/ip/uart/doc/registers.md"
    fields = []
    for name, body in re.findall(r"^## (\w+)\n(.*?)(?=^## |\Z)", path.read_text(), re.M | re.S):
        drawing = re.search(r"```wavejson\n(.*?)\n```", body, re.S)
        if name not in REGISTERS or not drawing:
            continue
        bit = 0
        for field in json.loads(drawing.group(1))["reg"]:
            if "name" in field:
                fields.append(
                    {
                        "register": name,
                        "name": field["name"],
                        "shift": bit,
                        "width": field["bits"],
                        "access": field["attr"][0],
                    }
                )
            bit += field["bits"]
    return fields


def field_values(field: dict) -> list[int]:
    special = {
        "CTRL.RXBLVL": list(range(4)),
        "CTRL.NCO": [0, 1, 0x2000, 0x3000, 0x4000, 0x5555, 0xAAAA, 0xFFFF],
        "FIFO_CTRL.RXILVL": list(range(7)),
        "FIFO_CTRL.TXILVL": list(range(5)),
        "TIMEOUT_CTRL.VAL": [0, 1, 0x555555, 0xAAAAAA, 0xFFFFFF],
    }
    return special.get(field["register"] + "." + field["name"], [0, 1])


def register_cases() -> list[dict]:
    result = []
    for name, (offset, mask, reset, reset_mask) in REGISTERS.items():
        for mode in ["offset", "reserved-read", "reserved-write"]:
            result.append(
                case(
                    f"UART-REG.{name}.{mode}",
                    "register",
                    register=name,
                    address=offset,
                    mask=mask,
                    mode=mode,
                )
            )
        if reset_mask:
            result.append(
                case(
                    f"UART-REG.{name}.reset-defined",
                    "register",
                    register=name,
                    address=offset,
                    mask=reset_mask,
                    expected=reset,
                    mode="reset-defined",
                )
            )
    for field in register_fields():
        identity = f"UART-REG.{field['register']}.{field['name']}"
        if field["access"] == "rw":
            for value in field_values(field):
                result.append(case(f"{identity}.rw.v{value}", "field", **field, value=value))
        elif field["access"] == "rw1c":
            for mode in ["w1c-zero", "w1c-one", "w1c-masked", "w1c-isolation"]:
                result.append(case(f"{identity}.{mode}", "field", **field, mode=mode))
        else:
            mode = "ro-write" if field["access"] == "ro" else "wo-read"
            result.append(case(f"{identity}.{mode}", "field", **field, mode=mode))
    return result


def bus_cases() -> list[dict]:
    result = []
    modes = [
        "request-stable",
        "response-stable",
        "single-outstanding",
        "accept-latency",
        "response-latency",
        "read-snapshot",
        "read-once",
        "write-once",
        "empty-read",
    ]
    result += [case("UART-BUS." + mode, "bus", mode=mode) for mode in modes]
    for mode in ["byte-masks", "w1c-masks", "wdata-strobe", "read-strobes"]:
        for strobe in range(16):
            result.append(case(f"UART-BUS.{mode}.s{strobe}", "bus", mode=mode, strobe=strobe))
    invalid = {0x34, 0x100, 0xFFFFFFFC}
    for offset, *_ in REGISTERS.values():
        invalid.update(offset + low for low in [1, 2, 3])
        invalid.update(offset | high for high in [0x100, 0x10000, 0x80000000])
    for address in sorted(invalid):
        for write in [False, True]:
            result.append(
                case(
                    f"UART-BUS.invalid.a{address:08x}.{'write' if write else 'read'}",
                    "invalid",
                    address=address,
                    write=write,
                )
            )
    return result


def serial_cases() -> list[dict]:
    result = []
    for direction in ["tx", "rx"]:
        for byte in range(256):
            result.append(
                case(
                    f"UART-TXRX.{direction}.b{byte:02x}",
                    direction,
                    payload=[byte],
                    nco=0x4000,
                    parity="disabled",
                )
            )
    for name, nco in [
        ("exact-a", 0x4000),
        ("exact-b", 0x2000),
        ("fractional", 0x3000),
        ("zero", 0),
    ]:
        result.append(
            case("UART-BAUD." + name, "baud", payload=[0x55, 0xAA], nco=nco, parity="disabled")
        )
    return result + parity_cases()


def parity_cases() -> list[dict]:
    result = []
    for mode in ["disabled", "even", "odd"]:
        for byte in [0, 1]:
            result.append(
                case(
                    f"UART-PARITY.{mode}.b{byte:02x}",
                    "parity",
                    parity=mode,
                    payload=[byte],
                    parity_class=byte.bit_count() & 1,
                    nco=0x4000,
                )
            )
    for mode in ["even", "odd"]:
        result.append(
            case(
                f"UART-PARITY.bad.{mode}",
                "parity-error",
                parity=mode,
                payload=[0],
                parity_class=0,
                nco=0x4000,
            )
        )
    for mode in ["back-to-back", "full-duplex", "enable-disable"]:
        result.append(
            case(
                "UART-TXRX." + mode,
                "traffic",
                mode=mode,
                payload=[0x55, 0xAA, 0, 255],
                nco=0x4000,
                parity="disabled",
                gap=0,
            )
        )
    return result


def fifo_cases() -> list[dict]:
    result = []
    for direction, levels in [("tx", [0, 1, 31, 32]), ("rx", [0, 1, 63, 64])]:
        for depth in levels:
            result.append(
                case(
                    f"UART-FIFO.{direction}.n{depth}",
                    "fifo",
                    direction=direction,
                    depth=depth,
                    payload=list(range(depth)),
                    nco=0x4000,
                )
            )
    for direction, thresholds in [("tx", [1, 2, 4, 8, 16]), ("rx", [1, 2, 4, 8, 16, 32, 62])]:
        for encoding, level in enumerate(thresholds):
            for depth in [level - 1, level, level + 1]:
                result.append(
                    case(
                        f"UART-WATERMARK.{direction}.e{encoding}.n{depth}",
                        "watermark",
                        direction=direction,
                        encoding=encoding,
                        depth=depth,
                        expected=depth < level if direction == "tx" else depth >= level,
                        payload=list(range(depth)),
                        nco=0x4000,
                    )
                )
    for mode in ["overflow", "order", "tx-reset", "rx-reset"]:
        result.append(
            case(
                "UART-FIFO." + mode,
                "fifo-behavior",
                mode=mode,
                payload=list(range(64)),
                nco=0x4000,
            )
        )
    return result


def peripheral_cases() -> list[dict]:
    result = []
    for bit in range(9):
        for mode in ["identity", "enable", "mask", "inject"]:
            result.append(
                case(
                    f"UART-IRQ.bit{bit}.{mode}",
                    "irq",
                    bit=bit,
                    mode=mode,
                    nco=0x4000,
                    payload=[0x55],
                )
            )
    for encoding in range(4):
        for parity in [0, 1]:
            result.append(
                case(
                    f"UART-ERROR.break.e{encoding}.p{parity}",
                    "break",
                    encoding=encoding,
                    parity="even" if parity else "disabled",
                    characters=[2, 4, 8, 16][encoding],
                    nco=0x4000,
                    payload=[0x55],
                )
            )
    for mode in [
        "disabled",
        "enabled",
        "read-depth-reset",
        "receive-depth-reset",
        "event-reset",
        "full-drop-no-reset",
        "w1c",
        "good-recovery",
    ]:
        result.append(
            case(
                "UART-ERROR.timeout." + mode,
                "timeout",
                mode=mode,
                value=32,
                bit_horizon=4096,
                nco=0x4000,
                payload=[0x55],
            )
        )
    return result + filter_and_reset_cases()


def filter_and_reset_cases() -> list[dict]:
    result = []
    for mode in ["noise", "false-start", "phase-start"]:
        for nf in [0, 1]:
            for phase in range(4):
                result.append(
                    case(
                        f"UART-FILTER.{mode}.nf{nf}.p{phase}",
                        "filter",
                        mode=mode,
                        nf=nf,
                        phase_quarters=phase,
                        payload=[0x55],
                        nco=0x4000,
                    )
                )
    groups = {
        "UART-ERROR": ("error", ["stop"]),
        "UART-LOOP": ("loop", ["system", "line"]),
        "UART-OVERRIDE": ("override", ["low", "high", "release"]),
        "UART-HISTORY": ("history", ["order"]),
        "UART-RESET": ("reset", ["idle", "active-tx", "active-rx", "occupied", "pending-mmio"]),
    }
    for family, (operation, modes) in groups.items():
        result += [
            case(f"{family}.{mode}", operation, mode=mode, payload=[0x55, 0xAA], nco=0x4000)
            for mode in modes
        ]
    return result


def seed_bytes(seed: str, family: str, index: int, count: int) -> bytes:
    """The reviewed LF-delimited SHA-256 stream, independent of library PRNGs."""
    blocks = []
    for block in range((count + 31) // 32):
        message = f"booley.qa.uart.seed.v1\n{seed}\n{family}\n{index:08x}\n{block:08x}\n"
        blocks.append(hashlib.sha256(message.encode("ascii")).digest())
    return b"".join(blocks)[:count]


def supplement(template: dict, family: str, seed: str, index: int) -> dict:
    item = copy.deepcopy(template)
    item["template_id"] = template["id"]
    item["id"] = PREFIX + f"{family}.seed.i{index:08x}"
    stream = seed_bytes(seed, family, index, 272)
    _, word1, word2, _ = struct.unpack("<4I", stream[:16])
    parameters = item["parameters"]
    payload = parameters.get("payload", [])
    parameters["payload"] = list(stream[16 : 16 + len(payload)])
    if family == "UART-BAUD" and parameters.get("nco"):
        parameters["nco"] = [0x4000, 0x2000, 0x3000][word1 % 3]
        parameters["payload"] = [0x55, 0xAA] if word2 % 2 == 0 else [0xAA, 0x55]
    if family == "UART-PARITY":
        byte = parameters["payload"][0]
        parameters["payload"][0] = byte ^ ((byte.bit_count() & 1) != parameters["parity_class"])
    if family == "UART-FILTER":
        parameters["phase_quarters"] = word1 % 4
    if family == "UART-TXRX" and parameters.get("mode") != "back-to-back":
        parameters["gap"] = word1 % 3
    if family == "UART-HISTORY":
        parameters["pattern"] = [byte & 1 for byte in stream[16:32]]
    item["evidence"] = {
        name: path.replace(template["id"], item["id"]) for name, path in item["evidence"].items()
    }
    return item


def evaluator_identity() -> str:
    """Hash source and control files in path order; generated evidence is excluded."""
    root = Path(__file__).resolve().parent
    files = sorted([*root.glob("*.py"), *root.glob("controls/*.sv")])
    digest = hashlib.sha256()
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def public_identity() -> dict:
    root = Path(__file__).resolve().parent.parent / "spec"
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def materialize(seed: str) -> dict:
    """Return all mandatory cases plus exactly eight supplements per serial family."""
    if not re.fullmatch("[0-9a-f]{32}", seed):
        raise ValueError("Seed must be exactly 32 lowercase hexadecimal characters")
    mandatory = register_cases() + bus_cases() + serial_cases() + fifo_cases() + peripheral_cases()
    cases = list(mandatory)
    for family in FAMILIES:
        templates = sorted(
            (item for item in mandatory if item["id"].startswith(PREFIX + family + ".")),
            key=lambda item: item["id"],
        )
        for index in range(8):
            word = int.from_bytes(seed_bytes(seed, family, index, 4), "little")
            cases.append(supplement(templates[word % len(templates)], family, seed, index))
    cases.sort(key=lambda item: item["id"])
    return {
        "format_version": 1,
        "protocol": "booley.qa.uart.seed.v1",
        "evaluator_sha256": evaluator_identity(),
        "public_inputs": public_identity(),
        "seed": seed,
        "cases": cases,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    manifest = materialize(args.seed)
    from publication import publish_new

    publish_new(args.output, manifest)


if __name__ == "__main__":
    main()
