"""Spawns and restarts the local llama.cpp process for whichever role this
node has been assigned -- the piece that eventually replaces the
hand-written launchd plists in ../launchd/ (see docs/ARCHITECTURE.md,
Phase 1 / GitHub issue #1).

Deliberately conservative for now: `supervise()` will restart a crashed
process, but nothing here touches launchd, and nothing runs unless
caravan_agent.py is explicitly told to (--apply). Scaffold stage --
proven correct in caravan_agent.py's dry-run output before this actually
drives the live setup.
"""
import subprocess
import time

import config


def build_head_command(model_blob_path: str, peer_rpc_addrs: list[str]) -> list[str]:
    """`peer_rpc_addrs` like ["100.90.134.95:50052"] -- Tailscale IPs
    preferred, per discovery.py and tonight's EHOSTUNREACH findings.

    Flags below match the production config proven live on 2026-09-06
    (docs/LOG.md: "kv_unified=false", "Raising -c to 262144", "Issue #4"):
    --parallel is required for --no-kv-unified to actually take effect
    (server.cpp silently forces kv_unified=true on the n_parallel<0 auto
    path otherwise -- see LOG.md, "no-kv-unified alone was silently
    ignored"), and -c must be n_slots * ctx_per_slot, not a flat
    ctx-per-slot value, or every slot silently gets a much smaller window
    than intended.
    """
    tensor_split = ",".join("1" for _ in range(len(peer_rpc_addrs) + 1))
    total_ctx = config.LLAMA_SERVER_N_SLOTS * config.LLAMA_SERVER_CTX_PER_SLOT
    cmd = [
        str(config.LLAMA_SERVER_BIN),
        "-m", model_blob_path,
    ]
    if peer_rpc_addrs:
        cmd += ["--rpc", ",".join(peer_rpc_addrs)]
        # This node's own device plus one RPC device per peer -- matches
        # tonight's proven `--device MTL0,RPC0` for a single peer. Only
        # correct for Metal-only nodes; revisit once a non-Mac tail
        # exists.
        devices = ["MTL0"] + [f"RPC{i}" for i in range(len(peer_rpc_addrs))]
        cmd += ["--device", ",".join(devices)]
    cmd += [
        "--tensor-split", tensor_split,
        "--port", str(config.LLAMA_SERVER_PORT),
        "-c", str(total_ctx),
        "--slot-save-path", str(config.SLOT_SAVE_DIR) + "/",
        "--parallel", str(config.LLAMA_SERVER_N_SLOTS),
        "--no-kv-unified",
    ]
    return cmd


def build_tail_command() -> list[str]:
    """--cache (GitHub issue #3, "solved natively") turns a restart from
    a 7-11 minute full re-transfer into a ~50-60s warm-cache load -- see
    docs/LOG.md. Only reason to ever pass RPC_SERVER_USE_CACHE=False is
    disk space on a node too small to hold a cached copy of its shard."""
    cmd = [
        str(config.RPC_SERVER_BIN),
        "-H", "0.0.0.0",
        "-p", str(config.RPC_PORT),
    ]
    if config.RPC_SERVER_USE_CACHE:
        cmd += ["--cache"]
    return cmd


def supervise(cmd: list[str], log_path, restart_delay: float = 10.0):
    """Runs `cmd`, restarting it if it exits, forever. Blocking -- run in
    its own thread/process. `log_path`: where stdout+stderr go (append
    mode), matching the launchd plists' StandardOutPath/StandardErrorPath
    convention so logs stay in a familiar place.

    Deliberately simple: no crash-loop backoff beyond the fixed
    `restart_delay`, no health checking beyond "did the process exit".
    Good enough to replace launchd's KeepAlive for phase 1; a smarter
    policy can come later if a real crash-loop shows up in practice.
    """
    log_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        with open(log_path, "a") as log_file:
            proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT)
            proc.wait()
        time.sleep(restart_delay)
