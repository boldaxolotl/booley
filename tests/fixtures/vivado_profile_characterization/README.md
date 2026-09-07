# Vivado profile characterization fixture

This fixture records the licensed-host proof used to select Booley's portable
FPGA PPA-profile mappings. It uses one tiny, deterministic out-of-context
counter design and an Artix-7 part available to the Vivado 2025.2 lane.

The checked-in `evidence.json` is the result captured on 07 SEP 2026. To repeat
the routed proof on an approved licensed host:

```bash
export BOOLEY_VIVADO_ROOT=/opt/Xilinx/2025.2
export BOOLEY_VIVADO_PROFILE_CHARACTERIZATION=1
pytest -m slow tests/flows/fpga/backends/vivado/test_profile_characterization.py
```

The opt-in test runs all three profiles. It never reads or prints license
configuration. The ordinary test lane validates the evidence and fixture
without launching Vivado.

This experiment proves that the named strategies are recognized, expand to the
captured vendor commands, preserve out-of-context mode, and complete routing.
It is not a general quality-of-results benchmark. In particular, the fixture
is too small to demonstrate a resource benefit for `compact`; FPGA area is
represented here by utilization counts, and power was not measured.
