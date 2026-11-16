# Request: close-test-gap

Close the coverage gap in the testbench: make the `gap` test also drive decoder
choice 2, while it still checks reset, counter wrap, choices 0 and 1, and every
assertion it has today.

- Change only `tb/coverage_tb.sv`. Leave the RTL, the core file, the test
  registry and `metrics.toml` as they are. Add no waivers and lower no
  bound.
- This Project's registry lists only `gap` for `sim_generated`, so the
  coverage table measures exactly `[gap]`.
- Collect coverage through MCP before and after the change. Ask the Coverage
  Analyst about the exact first Coverage Record.
- Keep the first collection's evidence (simulation passes, coverage fails),
  the Analyst's advice, and the final full Coverage Record.
- End with a clean, committed diff that touches only the testbench, and a
  Session Summary.

Give me a Measured Commit.
