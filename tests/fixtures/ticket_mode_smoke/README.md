# Ticket Mode production-image smoke fixture

This tiny Project is copied into a container-owned temporary directory by the
opt-in Ticket Mode smoke test. Its Targets exercise Verible lint, Icarus
elaboration and simulation, and Yosys/OpenROAD synthesis against the
setup-managed Nangate45 PDK mounted at `/opt/pdk`.

The fixture is not run by the ordinary host test suite. CI opts in with
`BOOLEY_TICKET_MODE_SMOKE=1` after building the production image.
