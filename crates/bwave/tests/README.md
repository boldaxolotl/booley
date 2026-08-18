# bwave test suite

All tests for the `bwave` crate live here, crate-local by design — they share the
fixtures in this directory and validate the Rust binary built from `../src`. They are
**not** collected by the repository's top-level `pytest` run (`testpaths = ["tests"]`
in the root `pyproject.toml`), nor by anything in the top-level `tests/` tree.

## Layout

| Path                              | Kind                | How to run |
|-----------------------------------|---------------------|------------|
| `integration_test.rs`             | Rust integration    | `cargo test` |
| `cli_issues.rs`                   | Rust regression     | `cargo test` |
| `property_tests.rs`               | Rust property tests (VCD→FST→query round-trip vs an independent model) | `cargo test` |
| `simulator_ground_truth_test.py`  | Python cross-check (Icarus/Verilator oracle vs. bwave) | `pytest tests/simulator_ground_truth_test.py` |
| `test_real_trace_fixtures.py`     | Python fixture check (compressed real VCDs)            | `pytest tests/test_real_trace_fixtures.py` |
| `native_fst_verilator_test.py`    | Native Verilator FST vs VCD+convert differential        | `pytest tests/native_fst_verilator_test.py` |
| `gen_test_vcds.py`                | Fixture generator (writes synthetic VCDs to `fixtures/`) | run directly |
| `fixtures/`                       | Tracked `.vcd` test inputs (incl. the `test_*.vcd` used by the Rust tests) | — |
| `rtl_fixtures/`                   | Verilog DUT + testbench sources for the ground-truth oracle | — |

## Why these aren't in the repo's `tests/` tree

The Python cross-validation tests are tightly coupled to this crate's fixture layout
(`fixtures/`, `rtl_fixtures/`, resolved relative to each file) and require RTL
simulators (Icarus/Verilator) plus the built `bwave` binary. They are run on demand
during bwave development, not as part of the Booley Python unit suite. Keeping them
beside the crate they exercise avoids splitting the fixtures and keeps the EDA tool
self-contained.
