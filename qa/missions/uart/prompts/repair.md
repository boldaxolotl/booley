# Request: repair

Repair the UART's conformance, attempt 1 of 2. An independent check found a
mismatch in the finished implementation; start from the integrated finished
commit. You have the original public contract and at most five diagnostic
excerpts. Reproduce each failure first, then add a regression test and fix the
RTL or testbench. Keep every bound met and every check as strong, and never
weaken one; the evaluator itself stays hidden. After the Measured Commit, the
full frozen case list is rerun with the same seed. This is a repair, not a
place to finish incomplete feature work.

Measure the base first (the feature commit), and run the RTL bugs, protocol,
spec (against `spec/contract.md`) and TB-quality Reviewers on `sim_uart`. Give
me a Measured Commit.
