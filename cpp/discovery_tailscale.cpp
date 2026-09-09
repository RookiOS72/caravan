#include "discovery_tailscale.h"

#include <sys/stat.h>

#include <cstdlib>
#include <cstring>
#include <sstream>

#include "json.hpp"
#include "subprocess.h"

using json = nlohmann::json;

namespace discovery::tailscale {

namespace {

// Homebrew-installed Tailscale.app puts the CLI here; not always on
// PATH. Falls back to a plain PATH lookup for other install methods --
// matches agent/discovery/tailscale.py's _CANDIDATE_BINARIES exactly.
const char * kAppBundlePath = "/Applications/Tailscale.app/Contents/MacOS/Tailscale";

bool file_exists(const std::string & path) {
    struct stat st;
    return stat(path.c_str(), &st) == 0;
}

// Minimal shutil.which("tailscale") equivalent: search $PATH for an
// executable file with that name.
std::optional<std::string> which(const std::string & name) {
    const char * path_env = std::getenv("PATH");
    if (!path_env) {
        return std::nullopt;
    }
    std::istringstream stream(path_env);
    std::string dir;
    while (std::getline(stream, dir, ':')) {
        std::string candidate = dir + "/" + name;
        struct stat st;
        if (stat(candidate.c_str(), &st) == 0 && (st.st_mode & S_IXUSR)) {
            return candidate;
        }
    }
    return std::nullopt;
}

std::optional<std::string> find_binary() {
    if (file_exists(kAppBundlePath)) {
        return std::string(kAppBundlePath);
    }
    return which("tailscale");
}

}  // namespace

bool is_available() {
    return find_binary().has_value();
}

std::vector<Peer> get_peers(double timeout_seconds) {
    auto binary = find_binary();
    if (!binary) {
        return {};
    }

    std::string output = subprocess::run_timeboxed(
        { *binary, "status", "--json" }, timeout_seconds);

    json data;
    try {
        data = json::parse(output);
    } catch (const json::parse_error & exc) {
        fprintf(stderr, "[caravan] tailscale.get_peers() failed (%s) -- falling back to mDNS/single-node\n",
                exc.what());
        return {};
    }

    // Bound to a named variable rather than chaining .items() straight off
    // data.value(...) -- .items() returns a proxy that holds a reference
    // to the object it's called on, and the .value() call's return is a
    // temporary that would otherwise be destroyed before the loop body
    // runs (a real, well-documented nlohmann::json pitfall -- caused a
    // real segfault here, caught by testing rather than assuming the
    // one-liner was safe).
    json peer_map = data.value("Peer", json::object());

    std::vector<Peer> peers;
    for (const auto & [key, peer] : peer_map.items()) {
        std::string ipv4;
        for (const auto & ip : peer.value("TailscaleIPs", json::array())) {
            std::string ip_str = ip.get<std::string>();
            if (ip_str.find('.') != std::string::npos) {
                ipv4 = ip_str;
                break;
            }
        }
        if (ipv4.empty()) {
            continue;
        }
        peers.push_back({
            peer.value("HostName", ""),
            ipv4,
            peer.value("Online", false),
        });
    }
    return peers;
}

}  // namespace discovery::tailscale
