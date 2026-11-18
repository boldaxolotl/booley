# Request: observability

Strengthen 10G MAC observability using new files only:
`qa/taxi_eth_mac_10g/test_observability.py` and `.sv`, plus ordinary Project
Target/test definitions for `sim_mac_10g_observability`. Reuse Setup's approved
64-bit, gearbox-disabled DIC/PTP/PFC/statistics contract, with the new thin
wrapper top and Cocotb module. Keep the Target selectable after integration.

Implement five deterministic registered tests:
- Bad RX FCS: corrupted FCS with unchanged payload; require RX error `tuser`,
  discrete bad-FCS indication and counter ID 34.
- Exact PFC: quanta [10,20,30,40,50,60,70,80]. Request each of the 8 classes
  alone (one-hot `tx_pfc_req`) and check the exact TX/RX bitmap and that
  class's quanta; then request all 8 together and check the exact bitmap and
  every quanta value. Check counters including IDs 25 and 57.
- Four-cycle underrun: XGMII error termination and counter ID 3.
- TX completion: unique 16-bit tags and timestamp relationship to observed SFD.
- Statistics: distinguish `tuser=0` counters from `tuser=1` string records and
  verify the ID namespace; never count strings as counters.

Do not edit existing Taxi files or weaken upstream tests.

Before you give me the commit, run `booley flow mutation --target
sim_mac_10g_observability --scope src/eth/rtl/taxi_eth_mac_10g.sv --count 8
--min-detected 7` and report its tally (the new tests must detect at least seven
of eight Mutants), and run a TB-quality Reviewer on the new Target. Adding the
`[sim_mac_10g_observability.sim]` table to `metrics.toml` needs my approval:
ask first. Give me a Measured Commit.
