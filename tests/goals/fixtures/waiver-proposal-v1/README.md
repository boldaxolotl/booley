# Legacy waiver proposal fixture

Captured with unchanged Booley code at commit
`025350e4c` using `prepared`, `propose`, and `interrupted_transaction` from
`tests/goals/test_goal_waivers.py` and `test_proposals.py`. The apply stopped
at the `approved` checkpoint, before writing the Change Log intent.

`payload.json`, `state.json` and `apply.json` are the original canonical bytes,
including the old producer's absolute paths beneath `/tmp/booley-1304-baseline`.
Do not regenerate them using the new binding codec. The recorded digests are
independent compatibility oracles. Recovery tests relocate the complete snapshot
and rebind its authority to the relocated bytes; they never need the capture
checkout or the original Campaign files.
