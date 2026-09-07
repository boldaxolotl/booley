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

}  // namespace

extern "C" void booley_coverage_start() {
    VerilatedCov::zero();
    events.push_back({"start", true});
    write_evidence();
}

extern "C" void booley_coverage_write() {
    const char* path = std::getenv("BOOLEY_COVERAGE_FILE");
    const bool success = path && *path;
    if (success) VerilatedCov::write(path);
    events.push_back({"write", success});
    write_evidence();
}
