// caravan (v0.1): the "no subprocess" unified binary from
// docs/ARCHITECTURE.md, now with real mDNS self-discovery instead of a
// hardcoded peer -- role detection, discover the other node on the LAN,
// then a direct in-process call into llama.cpp's own library entry
// points instead of spawning `llama-server`/`rpc-server` as children.
//
// Still narrow: no Tailscale discovery, no health server, no fast-path
// (Thunderbolt) selection yet -- those already work in the Python agent
// (see ../agent/) and are the next pieces to port. `--peer` stays
// available as a manual override for testing.
//
// Both role entry points are real, already-public llama.cpp APIs, not
// anything reimplemented here:
//   - head:  int llama_server(int argc, char** argv) -- exactly what
//            tools/server/main.cpp itself calls.
//   - tail:  void ggml_backend_rpc_start_server(...) -- a public API in
//            ggml-rpc.h, exactly what tools/rpc/rpc-server.cpp calls
//            after resolving devices.
#include <ggml-backend.h>
#include <ggml-rpc.h>

#include <signal.h>
#include <unistd.h>

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "discovery_mdns.h"
#include "discovery_tailscale.h"
#include "json.hpp"

using json = nlohmann::json;

// Matches agent/config.py -- kept in sync by hand for now (v0).
namespace config {
const std::string target_model = "muse-glimmer";
const int llama_server_port = 8080;
const int rpc_port = 50052;
const int n_slots = 4;
const int ctx_per_slot = 65536;
// Not actually listened on yet (no health server built here yet -- see
// agent/health_server.py for the piece this will eventually replace).
// Advertised now anyway so the value is already consistent once that
// piece is ported, rather than changing every peer's expected port
// later.
const int agent_port = 8091;
}  // namespace config

// Best-effort node-identity match, matching agent/caravan_agent.py's
// _normalize_hostname: strips a trailing ".local"/domain suffix and
// lowercases, so this node's own mDNS advertisement (e.g.
// "prf-hays-exo01-2.local") is recognized as itself and not treated as
// a peer -- the exact bug the Python port hit first (see docs/LOG.md,
// "mDNS fallback").
std::string normalize_hostname(const std::string & hostname) {
    std::string base = hostname.substr(0, hostname.find('.'));
    std::transform(base.begin(), base.end(), base.begin(),
                    [](unsigned char c) { return std::tolower(c); });
    return base;
}

std::string local_hostname() {
    char buf[256];
    if (gethostname(buf, sizeof(buf)) != 0) {
        return "";
    }
    return std::string(buf);
}

namespace ollama_store {

std::string home_dir() {
    const char * h = std::getenv("HOME");
    return h ? h : "";
}

// Mirrors agent/ollama_store.py's _manifest_path: "name" or "name:tag",
// defaulting to "latest" same as Docker.
std::string manifest_path(const std::string & model) {
    std::string name = model;
    std::string tag = "latest";
    auto colon = model.find(':');
    if (colon != std::string::npos) {
        name = model.substr(0, colon);
        tag  = model.substr(colon + 1);
    }
    return home_dir() + "/.ollama/models/manifests/registry.ollama.ai/library/" + name + "/" + tag;
}

bool has_model(const std::string & model) {
    std::ifstream f(manifest_path(model));
    return f.good();
}

// Returns the local GGUF blob path for `model`, or "" if not present --
// mirrors agent/ollama_store.py's resolve_blob_path.
std::string resolve_blob_path(const std::string & model) {
    std::ifstream f(manifest_path(model));
    if (!f.good()) {
        return "";
    }
    std::stringstream buf;
    buf << f.rdbuf();

    json manifest;
    try {
        manifest = json::parse(buf.str());
    } catch (const json::parse_error &) {
        return "";
    }

    for (const auto & layer : manifest.value("layers", json::array())) {
        if (layer.value("mediaType", "") == "application/vnd.ollama.image.model") {
            std::string digest = layer.value("digest", "");
            auto colon = digest.find(':');
            if (colon != std::string::npos) {
                digest[colon] = '-';
            }
            std::string blob_path = home_dir() + "/.ollama/models/blobs/" + digest;
            std::ifstream blob(blob_path);
            return blob.good() ? blob_path : "";
        }
    }
    return "";
}

}  // namespace ollama_store

// Auto-selects devices the same way tools/rpc/rpc-server.cpp's
// get_devices() does when no explicit -d/--device is given: prefer
// non-CPU accelerators, fall back to CPU only if none exist. Matches
// production's actual rpc-server invocation, which never passes -d.
std::vector<ggml_backend_dev_t> auto_select_devices() {
    std::vector<ggml_backend_dev_t> devices;
    for (size_t i = 0; i < ggml_backend_dev_count(); i++) {
        ggml_backend_dev_t dev = ggml_backend_dev_get(i);
        if (ggml_backend_dev_type(dev) != GGML_BACKEND_DEVICE_TYPE_CPU) {
            devices.push_back(dev);
        }
    }
    if (devices.empty()) {
        ggml_backend_dev_t dev = ggml_backend_dev_by_type(GGML_BACKEND_DEVICE_TYPE_CPU);
        if (dev) {
            devices.push_back(dev);
        }
    }
    return devices;
}

int llama_server(int argc, char ** argv);

int run_as_head(const std::string & blob_path, const std::string & peer) {
    std::string total_ctx = std::to_string(config::n_slots * config::ctx_per_slot);
    std::string tensor_split = peer.empty() ? "1" : "1,1";
    std::string rpc_addr = peer + ":" + std::to_string(config::rpc_port);

    std::vector<std::string> args = {
        "caravan",
        "-m", blob_path,
    };
    if (!peer.empty()) {
        args.push_back("--rpc");
        args.push_back(rpc_addr);
        args.push_back("--device");
        args.push_back("MTL0,RPC0");
    }
    args.push_back("--tensor-split");
    args.push_back(tensor_split);
    args.push_back("--port");
    args.push_back(std::to_string(config::llama_server_port));
    args.push_back("-c");
    args.push_back(total_ctx);
    args.push_back("--parallel");
    args.push_back(std::to_string(config::n_slots));
    args.push_back("--no-kv-unified");

    printf("[caravan] role: HEAD -- calling llama_server() in-process, no subprocess\n");
    printf("[caravan] args:");
    for (const auto & a : args) {
        printf(" %s", a.c_str());
    }
    printf("\n");

    std::vector<char *> argv;
    for (auto & a : args) {
        argv.push_back(a.data());
    }
    return llama_server((int)argv.size(), argv.data());
}

int run_as_tail() {
    ggml_backend_load_all();
    auto devices = auto_select_devices();
    if (devices.empty()) {
        fprintf(stderr, "[caravan] error: no backend devices found\n");
        return 1;
    }

    printf("[caravan] role: TAIL -- calling ggml_backend_rpc_start_server() in-process, no subprocess\n");
    printf("[caravan] devices:");
    for (auto dev : devices) {
        printf(" %s", ggml_backend_dev_name(dev));
    }
    printf("\n");

    std::string endpoint = "0.0.0.0:" + std::to_string(config::rpc_port);
    ggml_backend_rpc_start_server(endpoint.c_str(), nullptr, 4, devices.size(), devices.data());
    return 0;
}

namespace {
pid_t g_advertiser_pid = -1;

// Both role entry points block forever (that's what running a server
// means), so the only place cleanup can happen is a signal handler --
// mirrors agent/caravan_agent.py's `finally: advertiser.terminate()`,
// just reached via SIGTERM/SIGINT here instead of a Python return path.
// Without this, a stopped caravan process would leave a stale mDNS
// registration behind (see discovery_mdns.h's advertise_start doc).
void handle_shutdown_signal(int) {
    if (g_advertiser_pid > 0) {
        kill(g_advertiser_pid, SIGTERM);
    }
    _exit(0);
}
}  // namespace

int main(int argc, char ** argv) {
    // Unbuffered: printf output otherwise sits in libc's buffer
    // indefinitely once stdout isn't a tty (launchd logs, `> file`
    // redirection) -- the exact same class of issue hit repeatedly
    // tonight with Python's own stdout buffering under launchd.
    setvbuf(stdout, nullptr, _IONBF, 0);

    std::string peer;
    for (int i = 1; i < argc; i++) {
        if (std::strcmp(argv[i], "--peer") == 0 && i + 1 < argc) {
            peer = argv[++i];
        }
    }

    signal(SIGTERM, handle_shutdown_signal);
    signal(SIGINT, handle_shutdown_signal);

    std::string self_hostname = local_hostname();
    printf("[caravan] hostname=%s\n", self_hostname.c_str());

    g_advertiser_pid = discovery::mdns::advertise_start(self_hostname, config::agent_port);
    printf("[caravan] advertising via mDNS as '%s' (pid %d)\n", self_hostname.c_str(), g_advertiser_pid);

    if (peer.empty()) {
        auto discovered = discovery::mdns::discover_peers();
        printf("[caravan] discovered %zu mdns peer(s):\n", discovered.size());
        for (const auto & p : discovered) {
            printf("    mdns  %s  %s:%d\n", p.instance_name.c_str(), p.hostname.c_str(), p.port);
        }

        // Tailscale is not an independent trust source here -- there's
        // no health server yet (see discovery_tailscale.h) to verify a
        // Tailscale peer is actually running Caravan, not just some
        // other machine on the same tailnet (the exact bug the Python
        // agent's is_agent_alive gate exists to prevent -- see
        // agent/caravan_agent.py's docstring). Only used to *upgrade*
        // the address of a peer mDNS already validated by finding its
        // real _caravan._tcp advertisement, matching ARCHITECTURE.md's
        // stated Tailscale-over-LAN preference without reopening that
        // trust gap.
        auto ts_peers = discovery::tailscale::get_peers();
        printf("[caravan] discovered %zu tailscale peer(s) (address-upgrade only):\n", ts_peers.size());
        for (const auto & p : ts_peers) {
            printf("    tailscale  %s  %s  online=%s\n", p.hostname.c_str(), p.tailscale_ip.c_str(),
                   p.online ? "true" : "false");
        }

        for (const auto & p : discovered) {
            if (normalize_hostname(p.hostname) == normalize_hostname(self_hostname)) {
                continue;
            }
            peer = p.hostname;
            for (const auto & tp : ts_peers) {
                if (tp.online && normalize_hostname(tp.hostname) == normalize_hostname(p.hostname)) {
                    peer = tp.tailscale_ip;
                    printf("[caravan] upgraded %s to Tailscale IP %s\n", p.hostname.c_str(), peer.c_str());
                    break;
                }
            }
            break;
        }
    }

    bool have_model = ollama_store::has_model(config::target_model);
    printf("[caravan] target model: %s -- %s\n", config::target_model.c_str(),
           have_model ? "present locally" : "not present locally");

    int rc;
    if (have_model) {
        std::string blob_path = ollama_store::resolve_blob_path(config::target_model);
        if (blob_path.empty()) {
            fprintf(stderr, "[caravan] error: manifest found but GGUF blob missing\n");
            rc = 1;
        } else {
            rc = run_as_head(blob_path, peer);
        }
    } else {
        rc = run_as_tail();
    }

    if (g_advertiser_pid > 0) {
        kill(g_advertiser_pid, SIGTERM);
    }
    return rc;
}
