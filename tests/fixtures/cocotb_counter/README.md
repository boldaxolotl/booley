# Cocotb e2e fixture (plan-cocotb-support Part G)

A minimal Booley project with a cocotb Python testbench: an 8-bit counter DUT
(with deliberate `$error`/`$fatal` traps) and a 7-function cocotb module.
Six `.core` Targets share one build shape; only their `tests.toml` selected
sets differ, so each G-case is one `simulate` invocation.

The sandbox e2e runs **inside the sandbox image**, which pins cocotb. From the
repo root, with a freshly built image:

```bash
docker run --rm -v "$PWD/tests/fixtures/cocotb_counter":/work -w /work \
  booley-sandbox python3 -m booley.flows.simulate --work-dir /work --target <target>
```

| Case | Command | Expect |
|---|---|---|
| G8 happy ×2 | `--target sim_icarus` / `--target sim_verilator` | PASS, 3/3 per-test entries, one build + one sim process in the log |
| G9 failure | `--target sim_fail` | FAIL; `test_fail_assert` entry `fail` with the assertion text; siblings pass |
| G10 selection | `--target sim_icarus --test count` | runs exactly `test_count` |
| G10 bogus name | `--target sim_bad` | `test_bogus_name` inconclusive with the "no matching @cocotb.test" message |
| G11 crash shapes | `--target sim_crash` | never pass: `test_py_exception` fail (RuntimeError), `test_rtl_fatal` fail + SVA count > 0 |
| G11 timeout | `--target sim_hang --timeout 15000` | never pass; timeout verdict |
| G12 elaborate | `python3 -m booley.flows.elaborate --work-dir /work --target sim_verilator` | PASS (build half untouched) |
| G13 dry-run | `--target sim_icarus --dry-run` | one batched command: `fusesoc … --setup && make … && python3 -m booley.sim.cocotb_run … --test ×3` |
| G13 host guard | set `venue = "host"` | fail-fast "cocotb is sandbox-only in v1" |
| G14 trace | `--target sim_icarus --test count --trace` | TRACE_OK + queryable store; also on `sim_verilator` |
| G15 mutation smoke | flip `count + 1'b1` → `count - 1'b1` in the DUT, rerun `sim_icarus` | FAIL (the mutant is killed through the cocotb Target) |
