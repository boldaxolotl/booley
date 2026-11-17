- Rewrote the B-Wave docs served by `bwave docs` and `bwave skill` so each
  fact has one home. Command pages no longer repeat the flag lists `--help`
  prints, the skill's examples put the session alias first
  (`bwave @dut find ...`), and new `reference/patterns` and
  `reference/sessions` pages cover `-s` matching and `@<alias>` sessions.
  The `--format json` contract now lives only in `bwave schema`, whose
  descriptions gained the rules from the removed `reference/json-envelope`
  page, and `find`'s `data.value` is described correctly as normalized hex.
