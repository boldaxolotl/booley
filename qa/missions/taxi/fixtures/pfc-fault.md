### Seeded RTL fault

Recipe for the mission's `goal-repair` area. After the observability Goal finishes and both branches are integrated and you have saved a clean checkpoint, the operator changes only `src/eth/rtl/taxi_eth_mac_10g.sv`, on a run-owned branch listed in `resources.md`. At the `taxi_mac_pause_ctrl_tx` connection, replace:

```systemverilog
.tx_pfc_req(tx_pfc_req)
```

with:

```systemverilog
.tx_pfc_req({tx_pfc_req[6:0], tx_pfc_req[7]})
```

This rotates each requested priority by one while preserving the aggregate presence and count of PFC frames. The clean baseline and new oracle must pass before injection. After injection, the upstream count-only PFC test must still pass, while the new exact class/quanta test fails with class 0 observed as class 1 and a corresponding FST relationship. Save the seed commit, exact diff, both results, trace, B-Wave queries, and repository cleanliness under `evidence/goal-repair/`.

The fault location and repair are hidden from the Goal child. It receives the failing assertion, expected and observed class behavior, upstream-test contrast, affected file, and evidence pointers.
