# Vivado FPGA profile characterization — 07 SEP 2026

This report records the evidence used to choose Booley's portable FPGA
`ppa_profile` mappings. The experiment ran on licensed AMD Vivado 2025.2 build
6299465 with Edalize 0.6.8, the versions supported by Booley's current FPGA
lane.

## Selected mappings

| Portable intent | Vivado synthesis strategy | Vivado implementation strategy |
| --- | --- | --- |
| `compact` | `Flow_AreaOptimized_high` | `Area_Explore` |
| `balanced` | `Vivado Synthesis Defaults` | `Vivado Implementation Defaults` |
| `max_frequency` | `Flow_PerfOptimized_high` | `Performance_ExplorePostRoutePhysOpt` |

The complete strategy catalogs enumerated from the Vivado 2025.2 installation
are preserved in the fixture's `evidence.json`. Each selected pair was applied
to a project, synthesized out of context, implemented through its final enabled
step, and opened for timing and utilization reporting.

## Routed evidence

All runs used `xc7a35tcpg236-1`, an 8-bit counter, and a Target-owned 10 ns XDC
clock.

| Profile | Final implementation status | WNS (ns) | WHS (ns) | LUT | FF | BRAM | DSP |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `balanced` | `route_design Complete!` | 7.462 | 0.189 | 7 | 8 | 0 | 0 |
| `compact` | `route_design Complete!` | 7.462 | 0.189 | 7 | 8 | 0 | 0 |
| `max_frequency` | `phys_opt_design (Post-Route) Complete!` | 7.995 | 0.190 | 10 | 8 | 0 | 0 |

The generated run scripts also showed the expected strategy expansion. For
example, `compact` produced `synth_design -directive AreaOptimized_high` and
`opt_design -directive ExploreArea`; `max_frequency` produced performance
directives through post-route physical optimization. The complete captured
commands are in `evidence.json`.

## Adapter invariant discovered

Assigning the `synth_1` strategy resets Vivado synthesis-step properties,
including `STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS`. Booley must therefore apply
the profile strategy patch after Edalize creates the project but before its
existing out-of-context patch. Reversing those two patches silently drops
`-mode out_of_context`. The ordinary test binds the checked-in evidence to the
fixture's canonical UTF-8 text (LF line endings), while the licensed replay
observes the resulting Vivado property. The production adapter must carry its
own regression test when the public profile option lands.

## Interpretation

This is compatibility and transport evidence, not a broad QoR claim. It proves
that Vivado 2025.2 accepts the mappings, expands them to concrete vendor
commands, retains out-of-context synthesis, and completes the configured
implementation strategy on a deterministic fixture. The design is too small
to establish a general area advantage for `compact` or a general timing
advantage for `max_frequency`.

FPGA area is represented by utilization/resource metrics. Power was not
measured, so the portable profile name describes optimization intent rather
than a measured three-axis PPA result.

The reproducible fixture is
`tests/fixtures/vivado_profile_characterization`; the licensed run is opt-in so
normal CI never consumes a vendor seat.
