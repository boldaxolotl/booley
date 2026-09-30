#include "booley_coverage.h"

#include "verilated_cov.h"

#include <cstdlib>
#include <fstream>
#include <string>
#include <utility>
#include <vector>

namespace {

struct HookEvent final {
    std::string hook;
    bool success;
};

std::vector<HookEvent> events;

std::string json_string(const char* value) {
    std::string result = "\"";
    for (const char ch : std::string(value ? value : "")) {
        if (ch == '\\' || ch == '"') result += '\\';
        result += ch;
    }
    return result + "\"";
}

void write_evidence() {
    const char* path = std::getenv("BOOLEY_COVERAGE_HOOK_EVIDENCE");
    if (!path || !*path) return;
    std::ofstream stream(path, std::ios::out | std::ios::trunc);
    stream << "{\"$schema\":\"booley.coverage-hook/v1\",\"run_id\":"
           << json_string(std::getenv("BOOLEY_COVERAGE_RUN_ID")) << ",\"events\":[";
    for (std::size_t index = 0; index < events.size(); ++index) {
        if (index) stream << ',';
        stream << "{\"hook\":" << json_string(events[index].hook.c_str())
               << ",\"sequence\":" << index + 1 << ",\"success\":"
               << (events[index].success ? "true" : "false") << '}';
    }
    stream << "]}\n";
}

bool native_database_written(const char* path) {
    std::ifstream stream(path);
    std::string header;
    return std::getline(stream, header) && header == "# SystemC::Coverage-3";
}

}  // namespace

extern "C" void booley_coverage_start() {
    VerilatedCov::zero();
    events.push_back({"start", true});
    write_evidence();
}

extern "C" void booley_coverage_write() {
    const char* path = std::getenv("BOOLEY_COVERAGE_FILE");
    events.push_back({"write", false});
    write_evidence();
    if (!path || !*path) return;
    VerilatedCov::write(path);
    if (!native_database_written(path)) return;
    events.back().success = true;
    write_evidence();
}
