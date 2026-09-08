#include "Vtop.h"
#include "verilated.h"
#include "verilated_cov.h"
int main(int argc, char** argv) {
    VerilatedContext context;
    context.commandArgs(argc, argv);
    context.coveragep()->forcePerInstance(true);
    Vtop top{&context};
    top.clk = 0; top.a = 3; top.b = 3; top.c = 7; top.eval();
    top.clk = 1; top.eval();
    context.coveragep()->zero();  // deterministic post-reset boundary
    context.coveragep()->write("zero.dat");
    for (unsigned i = 0; i < 8; ++i) {
        top.clk = 0; top.a = i % 4; top.b = 0; top.c = i; top.eval();
        top.clk = 1; top.eval();
    }
    top.final();
    context.coveragep()->write();  // honors unique coverage-file plusarg
}
