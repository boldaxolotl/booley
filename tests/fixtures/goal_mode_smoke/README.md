# Goal Mode production-image smoke fixture

This tiny Project is copied into a container-owned temporary directory by the
opt-in Goal Mode smoke test. Its Targets exercise Verible lint, Icarus
elaboration and simulation, and Yosys/OpenROAD synthesis against the
setup-managed Nangate45 PDK mounted at `/opt/pdk`.

The fixture is not run by the ordinary host test suite. No CI job runs the
smoke since #3329; run it by hand inside the production image with
`BOOLEY_GOAL_MODE_SMOKE=1`.
