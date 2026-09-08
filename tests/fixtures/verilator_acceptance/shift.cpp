#include "Vtop.h"
#include "verilated.h"
#include <cstdint>
int main(int argc, char** argv) {
    VerilatedContext context;
    context.commandArgs(argc, argv);
    Vtop top{&context};
    for (uint32_t value : {0x80000001U, 0xdeadbeefU, 0xffffffffU}) {
        for (unsigned a = 0; a < 4; ++a) {
            for (unsigned b = 0; b < 4; ++b) {
                top.value = value; top.a = a; top.b = b; top.eval();
                if (top.left_shift != (value << (a + b))) return 1;
                if (top.right_shift != (value >> (a + b))) return 2;
            }
        }
    }
    top.final();
}
