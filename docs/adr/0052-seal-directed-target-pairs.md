# Seal directed Target pairs for relative QoR Criteria

Baseline-relative synthesis and FPGA Criteria use a directed pair of frozen Targets: the baseline runs at `base_sha`, the candidate runs at the ticket head, and a plain Target name remains the equal-pair shorthand. Booley seals the canonical direction in Target Contract schema 2 rather than allowing recipe edits, so tickets may intentionally add parameters, defines, or sources without giving the Developer Agent a way to change acceptance inputs opportunistically; schema 1 remains readable for legacy equal-Target tickets.
