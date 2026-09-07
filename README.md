# Caravan

*(Renamed from "llama-shard" — see [docs/LOG.md](docs/LOG.md) for the naming
discussion.)*

Experiment: shard a llama.cpp/GGUF model (the engine underneath Ollama) across
multiple Macs, the way [exo](https://github.com/exo-explore/exo) does for
MLX — but for the much larger llama.cpp/GGUF ecosystem instead of
Apple-Silicon-only MLX.

## Usage

Three processes, two machines, all supervised by launchd (`RunAtLoad` +
`KeepAlive` — they start automatically on login/reboot and restart
themselves on a crash, no manual intervention needed day to day):

| what | machine | label |
|---|---|---|
| `llama-server` (the sharded model) | node-a | `com.caravan.llama-server` |
| `slot_pin_proxy.py` (the proxy openclaw talks to) | node-a | `com.caravan.slot-pin-proxy` |
| `rpc-server` (node-b's compute half) | node-b | `com.caravan.rpc-server` |

All three plists are bootstrapped **directly from this repo's `launchd/`
directory** — there's no separate copy in `~/Library/LaunchAgents/` to
keep in sync. Editing a plist and re-bootstrapping (or editing
`proxy/slot_pin_proxy.py` and just restarting) always picks up the
current repo state.

### Check status

```sh
launchctl list | grep caravan                    # node-a
ssh brenden@<node-b-ip> 'launchctl list | grep caravan'   # node-b

curl http://127.0.0.1:8080/health   # llama-server directly
curl http://127.0.0.1:8090/health   # the proxy (what openclaw actually talks to)
curl http://127.0.0.1:8080/slots    # per-slot detail (busy/idle, token counts, cache hits)
```

Logs: `~/Library/Logs/caravan/*.log` on each machine.

### Restart

**`slot-pin-proxy`** (only the Python file changed — most commits): a
plain restart re-reads the script from disk, no plist changes needed.

```sh
launchctl kickstart -k gui/$(id -u)/com.caravan.slot-pin-proxy
```

**`llama-server`** (the plist's `ProgramArguments` changed — new flags,
model path, etc.) or **`rpc-server`**: `kickstart` alone won't pick up
plist edits — launchd needs to fully re-read the file:

```sh
launchctl bootout gui/$(id -u)/com.caravan.llama-server
launchctl bootstrap gui/$(id -u) ~/dev/caravan/launchd/com.caravan.llama-server.plist
```

Restarting `llama-server` triggers a full model reload. node-b's
`rpc-server` runs with `--cache` (a native llama.cpp feature — see
docs/LOG.md, "Issue #3: solved natively"), so as long as node-b's disk
cache is already warm this is now **~50-60 seconds**, not the
**~7-11 minutes** it takes cold (first run ever, or after a model/split
change that invalidates the cache; WiFi-bound shard transfer to node-b).
Either way, avoid it mid-conversation when possible.

**Never restart node-b's `rpc-server` while `llama-server` is running at
all** — not just "mid-transfer": `rpc-server` is stateless and holds
node-b's half of the model only in that process's own RAM, and
`llama-server` keeps a live RPC session against it for its entire
runtime, so restarting `rpc-server` at any point severs that connection
and crashes `llama-server` (see docs/LOG.md, "Self-inflicted crash").
Stop `llama-server` first, then restart `rpc-server`, then start
`llama-server` again — or just use the script that does this in the
right order automatically:

```sh
./scripts/restart-rpc-server.sh
```

It checks `/slots` is idle first (refuses to run unless you pass
`--force`), does the three-step sequence, and waits for `/health` to
report ready. Reaches for `ssh brenden@192.168.1.39` by default —
override with `CARAVAN_NODE_B_HOST`/`CARAVAN_NODE_B_USER` env vars if
node-b's address ever changes.

### Stop

```sh
launchctl bootout gui/$(id -u)/com.caravan.<label>
```

Removes it from launchd entirely — it will *not* come back on its own
(no more `KeepAlive`) until bootstrapped again.

### First-time setup on a new machine

See [launchd/README.md](launchd/README.md).

## Why

Ollama itself has no multi-machine sharding — it's deliberately single-node.
But the engine it's built on, llama.cpp, has a real (if less polished) RPC
backend (`ggml`'s `GGML_RPC` option) that lets you split a model's layers
across machines over the network. This project is step one of finding out
whether that's actually usable, before considering whether it's worth turning
into a proper app/open-source project.

## Status

Stages 1-2 (prove the mechanism, make it usable day-to-day) are done —
muse-glimmer runs sharded across both Macs and is openclaw's primary
model. Now on **stage 3, phase 1**: a self-organizing agent
(`agent/`) that figures out for itself whether a node is head or a tail,
replacing the hand-written per-role launchd plists in `launchd/`. See
[docs/PLAN.md](docs/PLAN.md) for the staged approach and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the current design.

## Hardware

Two Mac minis (Apple M4, 24GB unified memory each) on the same LAN,
referred to here as `node-a` (runs the orchestrating `llama-server`/
`llama-cli` process) and `node-b` (runs `rpc-server`). Both already run
[exo](https://github.com/exo-explore/exo) for MLX-based sharding — this
project is exploring the llama.cpp/GGUF equivalent.

## Log

See [docs/LOG.md](docs/LOG.md) for a running, dated log of what was tried,
what worked, and what didn't.
