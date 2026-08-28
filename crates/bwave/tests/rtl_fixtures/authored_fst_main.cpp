#include "Vauthored_fst_top.h"

#include "verilated.h"
#include "verilated_fst_c.h"

#include <iostream>

#ifndef VM_TRACE_FMT_FST
#error "the authored native-FST Target must select its VerilatedFstC harness"
#endif

int main(int argc, char** argv) {
    if (argc != 2) {
        std::cerr << "usage: Vauthored_fst_top OUTPUT.fst\n";
        return 2;
    }

    VerilatedContext context;
    context.commandArgs(argc, argv);
    Vauthored_fst_top top{&context};
    VerilatedFstC trace;
    context.traceEverOn(true);
    top.trace(&trace, 99);
    trace.open(argv[1]);

    top.clk = 0;
    top.rst_n = 0;
    for (int tick = 0; tick < 32; ++tick) {
        if (tick == 4) {
            top.rst_n = 1;
        }
        top.clk = !top.clk;
        top.eval();
        trace.dump(context.time());
        context.timeInc(1);
    }

    top.final();
    trace.close();
    std::cout << "[SIM_RESULT] PASSED\n";
    return 0;
}
