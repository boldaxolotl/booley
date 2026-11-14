# Synthesis profile characterization fixture

This fixture records the measurement behind Booley's ASIC synthesis
`ppa_profile` settings (`compact`, `balanced`, `max_frequency`). It uses the
picorv32 demo (RV32IMC `synth_core`, physical mode, Nangate45) at a 0.8 ns
clock, which `balanced` misses by 27 ps ("does not quite close").

`evidence.json` holds the `before` and `after` area, critical path, WNS, Fmax
and runtime of all three profiles, plus every candidate setting tried to pick
the `max_frequency` definition. `after.<profile>.settings` is checked against
the profile tables in the ordinary test lane, so a table edit without a
re-measurement fails `tests/flows/synth/test_profile_characterization.py`.

## Why `max_frequency` sets no delay target

The first idea was to map `max_frequency` with the SDC clock period as the ABC
delay target. It lost: ABC meets 800 ps in its wire-free model and recovers
area up to that target, so the placed netlist is slower (989.5 ps with the
`balanced` script, 872.2 ps for the best of seven scripts, against 826.9 ps
for `balanced` without a target). A target at or below ABC's minimum delay
maps exactly like no target. What does help is OpenROAD: gate cloning, repair
of every violating endpoint, a 0.2 ns setup margin and a roomier floorplan
(40% utilization, 0.70 density) take the critical path to 807.7 ps (-2.3%) for
+0.9% area. In logical mode, where OpenROAD does not run, `max_frequency`
therefore maps like `balanced`.

## Repeat it

Initialize the picorv32 demo project, set the `synth_core` SDC to the one in
this folder, then:

```bash
export BOOLEY_SYNTH_PROFILE_CHARACTERIZATION=1
export BOOLEY_PICORV32_PROJECT=/path/to/picorv32-demo
pytest -m slow tests/flows/synth/test_profile_characterization.py
```

The opt-in test runs each profile with `--no-cache` and checks area and
critical path within 2% of the `after` numbers (tool versions are in
`evidence.json`). This is one design at one clock period, evidence for the
ordering of profiles, not a general quality-of-results benchmark. Power was
not measured.
