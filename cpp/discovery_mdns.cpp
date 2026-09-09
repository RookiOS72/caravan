#include "discovery_mdns.h"

#include <regex>
#include <set>
#include <sstream>

#include "subprocess.h"

namespace discovery::mdns {

namespace {

const char * kServiceType = "_caravan._tcp";
const char * kDomain = "local";

// Matches agent/discovery/mdns.py's _ADD_LINE_RE.
const std::regex kAddLineRe(
    R"(^\s*[\d:.]+\s+Add\s+\d+\s+\d+\s+\S+\s+\S+\s+(.+?)\s*$)");

// Matches agent/discovery/mdns.py's _RESOLVE_LINE_RE.
const std::regex kResolveLineRe(R"(can be reached at (\S+?)\.?:(\d+))");

std::vector<std::string> split_lines(const std::string & text) {
    std::vector<std::string> lines;
    std::istringstream stream(text);
    std::string line;
    while (std::getline(stream, line)) {
        lines.push_back(line);
    }
    return lines;
}

}  // namespace

std::vector<std::string> browse(double duration_seconds) {
    std::string output = subprocess::run_timeboxed(
        { "dns-sd", "-B", kServiceType, kDomain }, duration_seconds);

    std::set<std::string> names;  // matches Python's sorted(set(...))
    for (const auto & line : split_lines(output)) {
        std::smatch m;
        if (std::regex_match(line, m, kAddLineRe)) {
            names.insert(m[1].str());
        }
    }
    return std::vector<std::string>(names.begin(), names.end());
}

std::optional<std::pair<std::string, int>> resolve(const std::string & instance_name,
                                                     double duration_seconds) {
    std::string output = subprocess::run_timeboxed(
        { "dns-sd", "-L", instance_name, kServiceType, kDomain }, duration_seconds);

    for (const auto & line : split_lines(output)) {
        std::smatch m;
        if (std::regex_search(line, m, kResolveLineRe)) {
            return std::make_pair(m[1].str(), std::stoi(m[2].str()));
        }
    }
    return std::nullopt;
}

std::vector<Peer> discover_peers(double browse_duration, double resolve_duration) {
    std::vector<Peer> peers;
    for (const auto & name : browse(browse_duration)) {
        auto resolved = resolve(name, resolve_duration);
        if (resolved) {
            peers.push_back({ name, resolved->first, resolved->second });
        }
    }
    return peers;
}

pid_t advertise_start(const std::string & instance_name, int port) {
    return subprocess::spawn_background(
        { "dns-sd", "-R", instance_name, kServiceType, kDomain, std::to_string(port) });
}

}  // namespace discovery::mdns
