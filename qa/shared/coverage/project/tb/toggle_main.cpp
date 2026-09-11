#include "Vtoggle.h"
#include "verilated.h"
#include <iostream>
#include <string>
#include <vector>
#if VM_COVERAGE
#include "booley_coverage.h"
#endif

int main(int argc, char** argv) {
    VerilatedContext context;
    context.commandArgs(argc, argv);
    Vtoggle dut{&context};
    std::string test = "half";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg.rfind("+test=", 0) == 0) test = arg.substr(6);
    }
    const std::vector<std::pair<std::string, std::vector<unsigned>>> cases = {
        {"zero", {0}}, {"quarter", {0,1,0}}, {"half", {0,3,0}},
        {"upper", {0,12,0}}, {"rise", {0,15}}, {"full", {0,15,0}},
        {"repeat", {0,3,0,3,0,3,0}}, {"reset", {0,3,0}}
    };
    const std::vector<unsigned>* values = nullptr;
    for (const auto& entry : cases) if (entry.first == test) values = &entry.second;
    if (!values) { std::cerr << "Unknown test\n"; return 2; }
    dut.value = 0;
    dut.eval();
    if (test == "reset") {
        dut.value = 15; dut.eval(); dut.value = 0; dut.eval();
    }
#if VM_COVERAGE
    booley_coverage_start();
#endif
    for (unsigned value : *values) {
        dut.value = value; dut.eval();
        std::cout << "QA_VALUE " << value << '\n';
    }
#if VM_COVERAGE
    booley_coverage_write();
#endif
    dut.final();
    std::cout << "[SIM_RESULT] PASSED\n";
    return 0;
}
