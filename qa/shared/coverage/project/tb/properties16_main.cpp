#include "Vproperties16.h"
#include "verilated.h"
#include <iostream>
#include <string>
#if VM_COVERAGE
#include "booley_coverage.h"
#endif
int main(int argc, char** argv) {
 VerilatedContext context; context.commandArgs(argc, argv); Vproperties16 dut{&context};
 std::string test="half";
 for(int i=1;i<argc;++i) { std::string a=argv[i]; if(a.rfind("+test=",0)==0) test=a.substr(6); }
 dut.clk=0; dut.value=0; dut.eval();
#if VM_COVERAGE
 booley_coverage_start();
#endif
 unsigned limit=test=="full" ? 16 : 2;
 for(unsigned i=0;i<limit;++i) {dut.clk=0;dut.value=i;dut.eval();dut.clk=1;dut.eval();}
#if VM_COVERAGE
 booley_coverage_write();
#endif
 dut.final(); std::cout << "[SIM_RESULT] PASSED\n";
}
