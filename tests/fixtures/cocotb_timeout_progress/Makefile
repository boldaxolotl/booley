SIM = icarus
TOPLEVEL_LANG = verilog
VERILOG_SOURCES = $(CURDIR)/capture.sv
COCOTB_TOPLEVEL = capture
# Select the scenario: mixed_cases or parametrized_cases.
COCOTB_TEST_MODULES ?= mixed_cases
include $(shell cocotb-config --makefiles)/Makefile.sim
