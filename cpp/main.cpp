// caravan (v0): proves the core "no subprocess" idea for the unified
// binary described in docs/ARCHITECTURE.md -- role detection, then a
// direct in-process call into llama.cpp's own library entry points
// instead of spawning `llama-server`/`rpc-server` as child processes.
//
// Deliberately narrow for now: no mDNS/Tailscale discovery, no health
// server, no fast-path selection -- those already work in the Python
// agent (see ../agent/) and are meant to be ported once this core
// mechanism is proven. Peer address is a plain CLI flag here.
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

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>

#include "json.hpp"

using json = nlohmann::json;

// Matches agent/config.py -- kept in sync by hand for now (v0).
namespace config {
const std::string target_model = "muse-glimmer";
const int llama_server_port = 8080;
const int rpc_port = 50052;
const int n_slots = 4;
const int ctx_per_slot = 65536;
}  // namespace config

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

int main(int argc, char ** argv) {
    std::string peer;
    for (int i = 1; i < argc; i++) {
        if (std::strcmp(argv[i], "--peer") == 0 && i + 1 < argc) {
            peer = argv[++i];
        }
    }

    bool have_model = ollama_store::has_model(config::target_model);
    printf("[caravan] target model: %s -- %s\n", config::target_model.c_str(),
           have_model ? "present locally" : "not present locally");

    if (have_model) {
        std::string blob_path = ollama_store::resolve_blob_path(config::target_model);
        if (blob_path.empty()) {
            fprintf(stderr, "[caravan] error: manifest found but GGUF blob missing\n");
            return 1;
        }
        return run_as_head(blob_path, peer);
    }
    return run_as_tail();
}
