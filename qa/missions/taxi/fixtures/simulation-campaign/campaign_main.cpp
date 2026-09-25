#include "Vcampaign_tb.h"
#include "verilated.h"

#include <chrono>
#include <fstream>
#include <string>
#include <thread>

namespace {

std::string selected_test(int argc, char **argv) {
    constexpr const char *prefix = "+test=";
    for (int index = 1; index < argc; ++index) {
        std::string argument(argv[index]);
        if (argument.rfind(prefix, 0) == 0) {
            return argument.substr(std::char_traits<char>::length(prefix));
        }
    }
    return "";
}

void exercise_parallel_attempt(const std::string &test) {
    if (test.rfind("slow-", 0) != 0) {
        return;
    }
    std::ofstream marker("qa-shared-name.txt", std::ios::trunc);
    marker << test << '\n';
    marker.close();
    std::this_thread::sleep_for(std::chrono::milliseconds(250));
}

}  // namespace

int main(int argc, char **argv) {
    Verilated::commandArgs(argc, argv);
    exercise_parallel_attempt(selected_test(argc, argv));
    Vcampaign_tb top;
    while (!Verilated::gotFinish()) {
        top.eval();
        Verilated::timeInc(1);
    }
    top.final();
    return 0;
}
