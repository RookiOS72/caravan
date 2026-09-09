// LAN peer discovery via Bonjour/mDNS, using macOS's built-in `dns-sd`
// CLI -- a direct C++ port of agent/discovery/mdns.py, same tool, same
// regexes, same reasoning (see that file's docstring for the full
// rationale: dns-sd talks to the same system mDNSResponder daemon a
// native NWBrowser would, no third-party dependency needed).
#pragma once

#include <optional>
#include <string>
#include <vector>

namespace discovery::mdns {

struct Peer {
    std::string instance_name;
    std::string hostname;
    int port;
};

// Instance names of every Caravan agent advertising on the LAN right
// now, found by listening for `duration_seconds`.
std::vector<std::string> browse(double duration_seconds = 3.0);

// Hostname/port for one previously-browsed instance name.
std::optional<std::pair<std::string, int>> resolve(const std::string & instance_name,
                                                     double duration_seconds = 2.0);

// Browse, then resolve each found instance in turn.
std::vector<Peer> discover_peers(double browse_duration = 3.0, double resolve_duration = 2.0);

// Registers this node's Caravan agent as discoverable. Long-running --
// caller owns the returned pid and must kill() it on shutdown (dns-sd
// deregisters on process exit).
pid_t advertise_start(const std::string & instance_name, int port);

}  // namespace discovery::mdns
