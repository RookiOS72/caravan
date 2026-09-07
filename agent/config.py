"""Tunables for the Caravan agent -- paths, ports, and the current target
model. See docs/ARCHITECTURE.md for why these values are what they are.
"""
from pathlib import Path

# Where each agent instance persists its own identity (see identity.py).
# Deliberately outside the git repo -- this is runtime state, not source.
STATE_DIR = Path.home() / ".caravan" / "agent"

# The model this cluster currently serves. Hardcoded for now -- Caravan
# supports one real conversation identity today (see openclaw.json), so
# one target model is the correct-sized scaffold. Revisit if/when
# multi-model support becomes real usage, not just a nice side effect of
# the self-declaration design (see ARCHITECTURE.md).
TARGET_MODEL = "muse-glimmer"

# llama.cpp binaries -- same paths used by the hand-written launchd
# plists in ../launchd/. Not yet unified with those plists; this agent is
# meant to eventually replace them (see ARCHITECTURE.md, issue #1).
LLAMA_SERVER_BIN = Path.home() / "dev" / "llama.cpp" / "build" / "bin" / "llama-server"
RPC_SERVER_BIN = Path.home() / "dev" / "llama.cpp" / "build" / "bin" / "ggml-rpc-server"

# Ports. RPC_PORT and LLAMA_SERVER_PORT match tonight's proven manual
# setup. AGENT_PORT is a small HTTP surface each agent exposes for peer
# liveness checks (mDNS/Tailscale discovery both need something to
# actually probe) -- see health_server.py.
RPC_PORT = 50052
LLAMA_SERVER_PORT = 8080
AGENT_PORT = 8091

# n_slots x LLAMA_SERVER_CTX_PER_SLOT is llama-server's actual -c value
# (each slot gets a full independent window, not a shared pool split
# n_slots ways) -- see docs/LOG.md, "kv_unified=false" and "The 16K-
# per-slot cap was a real regression" for why this specific shape, not
# just a flat context size. Real sessions have hit 40K+ tokens; a shared/
# split pool silently truncates those.
LLAMA_SERVER_N_SLOTS = 4
LLAMA_SERVER_CTX_PER_SLOT = 65536

# Disk-persisted KV cache slots (GitHub issue #4) -- lets a restart reuse
# a session's already-processed prompt instead of reprocessing it cold.
SLOT_SAVE_DIR = Path.home() / ".caravan" / "llama-server" / "slots"

# rpc-server's own on-disk tensor cache (GitHub issue #3) -- turns a
# restart from a 7-11 minute full re-transfer into a ~50-60s warm-cache
# load. Native llama.cpp feature (rpc-server --cache), not something
# Caravan built -- see docs/LOG.md, "Issue #3: solved natively."
RPC_SERVER_USE_CACHE = True

# mDNS/Bonjour service type this agent advertises and browses for.
MDNS_SERVICE_TYPE = "_caravan._tcp"
MDNS_DOMAIN = "local"
