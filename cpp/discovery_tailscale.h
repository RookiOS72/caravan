// Tailscale-based peer discovery -- a direct C++ port of
// agent/discovery/tailscale.py. See that file's docstring for why
// Tailscale IPs are preferred over LAN IPs (tonight's EHOSTUNREACH
// findings, see docs/ARCHITECTURE.md), and docs/LOG.md's "Tailscale
// discovery fails under launchd" for the real caveat this inherits
// unchanged: the App-bundled CLI's IPC to the running GUI app can fail
// specifically under launchd, so mDNS (discovery_mdns.h) is what
// actually makes discovery work in that context, not this.
#pragma once

#include <optional>
#include <string>
#include <vector>

namespace discovery::tailscale {

struct Peer {
    std::string hostname;
    std::string tailscale_ip;
    bool online;
};

// True if a Tailscale CLI binary (App-bundled or PATH) is present at all.
bool is_available();

// Every peer in this node's tailnet, online or not -- callers should
// filter on `.online` themselves. Empty if Tailscale isn't installed,
// isn't running, or the CLI call fails for any reason (see
// discovery_tailscale.cpp -- logged, not silent, matching the Python
// version).
std::vector<Peer> get_peers(double timeout_seconds = 5.0);

}  // namespace discovery::tailscale
