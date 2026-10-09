"""Shared Stealth design inputs for worktree and Goal regressions."""

CORE = """CAPI=2:
name: ::top:0
filesets:
  rtl: {files: [rtl.v], file_type: verilogSource}
  constraints:
    files: [.booley_project/cores/constraints/top.sdc]
    file_type: SDC
targets:
  top: {filesets: [rtl, constraints], toplevel: top, default_tool: verilator}
""".replace(" targets:", "targets:")
