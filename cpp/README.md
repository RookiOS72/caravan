# caravan (C++, v0.1)

The start of the unified-binary vision from `docs/ARCHITECTURE.md`: one
Caravan app, no subprocess spawning -- `llama.cpp`'s own library entry
points called directly, in-process, instead of the current `agent/`
(Python) spawning `llama-server`/`rpc-server` as children under
supervision.

## Status

Real, tested (not just compiles):

- **Head**: calls `int llama_server(int argc, char** argv)` directly --
  exactly what `tools/server/main.cpp` itself calls. Verified: the HTTP
  server comes up inside `caravan`'s own process, answers `/health`, and
  serves real completions when there's enough memory for the model
  (single-node testing hit a genuine GPU OOM running the full 30B model
  on one machine -- expected, this model is normally sharded for exactly
  that reason, not a bug in this code).
- **Tail**: calls `void ggml_backend_rpc_start_server(...)` directly --
  exactly what `tools/rpc/rpc-server.cpp` calls after resolving devices.
  Verified: a real TCP listener comes up, accepts connections, all
  inside `caravan`'s own process.
- Confirmed via `ps` during testing: only one process (`caravan`
  itself) ever exists, no `llama-server`/`rpc-server` child anywhere.
- **mDNS self-discovery**: `discovery_mdns.{h,cpp}` is a direct C++ port
  of `agent/discovery/mdns.py` (same `dns-sd` CLI, same regexes, same
  browse-then-resolve-then-advertise shape), backed by `subprocess.{h,cpp}`
  -- a small POSIX fork/exec/pipe/poll utility for `dns-sd`'s "streams
  forever, no single-result mode" behavior. Verified live: `caravan`
  advertises itself, discovers both itself and the real peer, correctly
  excludes itself (`normalize_hostname`, the exact bug the Python port
  hit first), and builds the full sharded `llama_server()` argv
  automatically -- `--peer` is no longer required, only kept as a manual
  override for testing.

**Not done yet** -- everything else `agent/` already does in Python:
Tailscale discovery, the `/health` + `fast_paths` server, the
auto-select-fastest-path logic, persistent node identity.

Ollama model detection (`ollama_store::has_model`/`resolve_blob_path`)
is a straight port of `agent/ollama_store.py`'s logic, using the JSON
library already vendored in `llama.cpp/vendor/nlohmann` -- no new
dependency.

## Building

Requires an already-built `llama.cpp` checkout (defaults to
`~/dev/llama.cpp`, override with `-DLLAMA_CPP_DIR=...`). This is a
temporary arrangement -- proper vendoring (a submodule pinned to the
`RookiOS72/llama.cpp` fork's `caravan-patches` branch, built as part of
this project) is the real "step 1" work, deferred until this core
mechanism was proven worth building on.

```sh
cmake -B build -S .
cmake --build build
./build/caravan                 # role + peer auto-detected
./build/caravan --peer <ip>     # manual override, skips mDNS discovery
```

**Testing note**: `llama_server_port`/`rpc_port` in `main.cpp` are the
real production values (8080/50052) -- when testing against a live
production node, temporarily point at scratch ports on *both* the local
bind and the discovered peer's RPC port, not just the local one. A v0.1
test connected to node-b's real production `rpc-server` by accident
(only the local port had been overridden) -- harmless this time (a
quick connect/disconnect, confirmed via node-b's logs and a real
completion request afterward), but avoid relying on that.
