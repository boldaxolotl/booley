"""Required Verilator image gate: run directly with Python inside the candidate.

No optional-tool skips. All builds/runs are bounded and native files remain in
--work-dir for inspection. This probes the upstream format; it is not Booley's
future coverage adapter or a scoring implementation.
"""

from __future__ import annotations

import argparse
import re
import signal
import subprocess
import sys
import unittest
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "verilator_acceptance"
FLAGS = [
    "--coverage-line",
    "--coverage-toggle",
    "--coverage-expr",
    "--coverage-user",
    "--coverage-per-instance",
]
REVISION = "ea338be98e1e838d3518809ce8899f85a009963c"
WORK = Path("/tmp/verilator-acceptance")


def run(args: list[str], cwd: Path, *, timeout: int = 300, success: bool = True):
    """Run a bounded command, retaining output even when it fails."""
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
    )
    with (cwd / "commands.log").open("a") as log:
        log.write(f"{args!r}\n{result.stdout}\n{result.stderr}\n")
    if success and result.returncode:
        raise AssertionError(f"{args!r}\n{result.stdout}\n{result.stderr}")
    return result


def records(path: Path) -> dict[str, int]:
    """Strict test-only reader retaining the full native key, without scoring."""
    lines = path.read_text().splitlines()
    assert lines[0] == "# SystemC::Coverage-3", lines[:1]
    result = {}
    for line in lines[1:]:
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"C '(.*)' (\d+)", line)
        assert match, repr(line)
        key, count = match.groups()
        assert key not in result, key
        result[key] = int(count)
    assert result, path
    return result


def metadata(key: str) -> dict[str, str]:
    return dict(part.split("\x02", 1) for part in key.split("\x01") if "\x02" in part)


class Acceptance(unittest.TestCase):
    def setUp(self) -> None:
        self.work = WORK / self._testMethodName
        self.work.mkdir(parents=True, exist_ok=True)

    def build(self, source: str, *, main: str | None = None, coverage: bool = False):
        args = ["verilator", "--top-module", "top", "--Mdir", "obj", "-Wno-fatal"]
        args += ["--cc", "--exe", "--build"] if main else ["--binary", "--timing"]
        if coverage:
            args += FLAGS
        args += [str(FIXTURES / source)]
        if main:
            args += [str(FIXTURES / main)]
        run(args, self.work)
        return str(self.work / "obj" / "Vtop")

    def test_00_identity(self) -> None:
        version = run(["verilator", "--version"], self.work).stdout
        self.assertRegex(version, r"Verilator 5\.052\b")
        provenance = Path("/usr/local/share/verilator/BOOLEY-SOURCE.txt").read_text()
        self.assertIn(REVISION, provenance)
        self.assertIn("v5.052", provenance)
        self.assertTrue(Path("/usr/include/lz4.h").is_file())
        self.assertEqual(run(["cocotb-config", "--version"], self.work).stdout.strip(), "2.1.0")
        libraries = run(["ldd", "/usr/local/bin/verilator_bin"], self.work).stdout
        self.assertNotIn("jemalloc", libraries)

    def test_01_nested_shift_custom_untimed(self) -> None:
        executable = self.build("shift.sv", main="shift.cpp")
        run([executable], self.work, timeout=30)

    def test_02_force_unpacked_timed(self) -> None:
        run([self.build("force.sv")], self.work, timeout=30)

    def test_03_lint_elaboration_and_hard_diagnostic(self) -> None:
        for mode in ("--lint-only", "--cc"):
            run(["verilator", mode, "--top-module", "top", str(FIXTURES / "shift.sv")], self.work)
        invalid = self.work / "invalid.sv"
        invalid.write_text("module top; final begin #1; end endmodule\n")
        failed = run(
            ["verilator", "--lint-only", "--timing", "-Wno-fatal", str(invalid)],
            self.work,
            success=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("Delays are not legal in final blocks", failed.stderr)

    def test_04_generated_seed_verdicts_and_destination(self) -> None:
        executable = self.build("generated.sv", coverage=True)
        outputs = []
        for name in ("first.dat", "second.dat"):
            result = run(
                [executable, "+verilator+seed+123", f"+verilator+coverage+file+{name}"],
                self.work,
                timeout=30,
            )
            outputs.append(re.search(r"RANDOM=\d+", result.stdout).group())
            records(self.work / name)
        self.assertEqual(*outputs)
        self.assertFalse((self.work / "coverage.dat").exists())
        failed = run(
            [executable, "+fail", "+verilator+coverage+file+failed.dat"],
            self.work,
            timeout=30,
            success=False,
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("intentional failure", failed.stdout + failed.stderr)
        with self.assertRaises(subprocess.TimeoutExpired):
            run([executable, "+hang"], self.work, timeout=1)
        with subprocess.Popen(
            [executable, "+hang"],
            cwd=self.work,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as child:
            try:
                child.wait(timeout=0.2)
                self.fail("hang fixture exited before termination")
            except subprocess.TimeoutExpired:
                child.terminate()
                self.assertEqual(child.wait(timeout=5), -signal.SIGTERM)

    def test_05_native_per_instance_reset_and_merge(self) -> None:
        executable = self.build("coverage source.sv", main="coverage.cpp", coverage=True)
        for name in ("first.dat", "second.dat"):
            run([executable, f"+verilator+coverage+file+{name}"], self.work, timeout=30)
        self.assertTrue(all(count == 0 for count in records(self.work / "zero.dat").values()))
        first = records(self.work / "first.dat")
        second = records(self.work / "second.dat")
        self.assertEqual(first, second)
        self.assertIn(0, first.values())
        self.assertTrue(any(first.values()))
        points = [(metadata(key), count) for key, count in first.items()]
        types = {point.get("t") for point, _ in points}
        self.assertTrue({"line", "branch", "expr", "toggle", "user"} <= types, types)
        self.assertFalse({"fsm_state", "fsm_arc", "covergroup"} & types)
        hierarchy = {point.get("h", "") for point, _ in points}
        for instance in ("same_a", "same_b", "different"):
            self.assertTrue(any(instance in name for name in hierarchy), hierarchy)
        properties = [(point["h"], count) for point, count in points if point.get("t") == "user"]
        self.assertTrue(any("same_a" in h and count > 0 for h, count in properties), properties)
        self.assertTrue(any("same_b" in h and count == 0 for h, count in properties), properties)
        self.assertTrue(any("coverage source.sv" in point.get("f", "") for point, _ in points))
        toggles = [point for point, _ in points if point.get("t") == "toggle"]
        comments = {point.get("o", "") for point in toggles}
        self.assertTrue(any("0->1" in comment for comment in comments), comments)
        self.assertTrue(any("1->0" in comment for comment in comments), comments)
        run(["verilator_coverage", "--write", "merged.dat", "first.dat", "second.dat"], self.work)
        expected = Counter(first)
        expected.update(second)
        self.assertEqual(records(self.work / "merged.dat"), dict(expected))
        for report in ("summary", "hier"):
            output = run(["verilator_coverage", "--report", report, "merged.dat"], self.work)
            self.assertTrue(output.stdout.strip())
        # Original files retain per-test incidence after native aggregation.
        self.assertEqual(records(self.work / "first.dat"), first)

    def test_07_generated_per_instance(self) -> None:
        run(
            [
                "verilator",
                "--binary",
                "--timing",
                "--top-module",
                "harness",
                "--Mdir",
                "obj",
                "-Wno-fatal",
                *FLAGS,
                str(FIXTURES / "coverage source.sv"),
                str(FIXTURES / "generated_coverage.sv"),
            ],
            self.work,
        )
        run(
            [str(self.work / "obj" / "Vharness"), "+verilator+coverage+file+generated.dat"],
            self.work,
            timeout=30,
        )
        points = records(self.work / "generated.dat")
        hierarchy = {metadata(key).get("h", "") for key in points}
        self.assertFalse(any("*" in name for name in hierarchy), hierarchy)
        for instance in ("same_a", "same_b", "different"):
            self.assertTrue(any(instance in name for name in hierarchy), hierarchy)

    def test_06_cocotb_seed_and_native_destination(self) -> None:
        logs = []
        for index in (1, 2):
            output = run(
                [
                    sys.executable,
                    str(FIXTURES / "cocotb_driver.py"),
                    f"cocotb-{index}.dat",
                    f"results-{index}.xml",
                ],
                self.work,
            )
            result = ET.parse(self.work / f"results-{index}.xml").getroot()
            self.assertEqual(len(result.findall(".//testcase")), 1)
            self.assertEqual(result.findall(".//failure"), [])
            self.assertEqual(result.findall(".//error"), [])
            points = records(self.work / f"cocotb-{index}.dat")
            hierarchy = {metadata(key).get("h", "") for key in points}
            self.assertFalse(any("*" in name for name in hierarchy), hierarchy)
            for instance in ("same_a", "same_b", "different"):
                self.assertTrue(any(instance in name for name in hierarchy), hierarchy)
            logs.append(re.search(r"RANDOM=\[[^\n]+", output.stdout).group())
        self.assertEqual(*logs)
        self.assertEqual(records(self.work / "cocotb-1.dat"), records(self.work / "cocotb-2.dat"))
        self.assertFalse((self.work / "coverage.dat").exists())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    WORK = args.work_dir.resolve()
    unittest.main(argv=[sys.argv[0]], verbosity=2)
