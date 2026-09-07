#!/usr/bin/env python3
"""caravan-agent: the self-organizing piece of Caravan.

One binary/script, meant to run identically on every node. On each run it
decides for itself whether this machine should act as head (serves the
target model, needs it pulled locally) or a tail (contributes compute to
whichever machine is head) -- see docs/ARCHITECTURE.md for the full
design and why this doesn't need a real election protocol.

Status: scaffold. Role detection and both discovery paths are real and
testable today. Actually spawning/supervising llama-server or rpc-server
only happens with --apply -- default is a dry run that prints the plan,
so this can be exercised safely alongside the still-live manually-managed
setup (../launchd/) without risking it. Not yet wired into launchd itself
-- see ARCHITECTURE.md's open questions.

Usage:
    python3 caravan_agent.py                  # dry run, one pass
    python3 caravan_agent.py --apply           # actually spawn+supervise
    python3 caravan_agent.py --model llama3    # target a different model
"""
import argparse
import http.client
import sys
import time
from pathlib import Path

import config
import health_server
import identity
import ollama_store
import supervisor
from discovery import mdns, tailscale


def is_agent_alive(ip: str, timeout: float = 1.5) -> bool:
    """True only if a Caravan agent's health endpoint actually answers at
    `ip`:AGENT_PORT -- the gate that stops "some other machine happens to
    be reachable" from being trusted as a real tail. Without this,
    Tailscale discovery in particular will find every peer in the
    tailnet, Caravan or not -- confirmed directly: a dry run tonight
    found an unrelated tailnet machine (prf-hays-mgmt) and would have
    wired it into --rpc as if it were a tail. See health_server.py.
    """
    try:
        conn = http.client.HTTPConnection(ip, config.AGENT_PORT, timeout=timeout)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        ok = resp.status == 200
        conn.close()
        return ok
    except OSError as exc:
        print(f"[caravan-agent] is_agent_alive({ip!r}) failed: {exc!r}")
        return False


def discover_peers(agent_id: str) -> list[dict]:
    """Merges Tailscale + mDNS sightings into one peer list, preferring
    the Tailscale IP for a peer found both ways (see ARCHITECTURE.md),
    and keeping only peers that actually answer as a Caravan agent (see
    is_agent_alive above) -- a peer that's merely *reachable* isn't
    necessarily *ours*.

    Reconciling an mDNS sighting with a Tailscale one by node identity
    isn't implemented yet -- this agent doesn't publish its own node_id
    in an mDNS TXT record yet, so mDNS-found peers are reported as-is
    (LAN IP only) and simply supplement, rather than get merged with,
    Tailscale peers. Good enough for a first pass: on tonight's 2-node
    LAN, a peer found via Tailscale is unambiguous already.
    """
    peers = []

    for peer in tailscale.get_peers():
        if not peer.online or not is_agent_alive(peer.tailscale_ip):
            continue
        peers.append({
            "source": "tailscale",
            "hostname": peer.hostname,
            "ip": peer.tailscale_ip,
        })

    for peer in mdns.discover_peers():
        if not is_agent_alive(peer.hostname):
            continue
        peers.append({
            "source": "mdns",
            "hostname": peer.hostname,
            "ip": peer.hostname,  # resolved hostname, not yet a bare IP
            "port": peer.port,
        })

    return peers


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=config.TARGET_MODEL)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually spawn+supervise the local process. Default is dry-run.",
    )
    parser.add_argument(
        "--serve-for", type=float, default=0.0, metavar="SECONDS",
        help="After printing the plan, keep the health endpoint up for this "
             "many seconds instead of exiting immediately -- lets another "
             "node's dry run see this one as alive. For testing discovery "
             "across two machines; --apply implies staying up already.",
    )
    args = parser.parse_args()

    me = identity.load_or_create()
    print(f"[caravan-agent] node_id={me['node_id']} hostname={me['hostname']}")

    health_server.start(me["node_id"])
    print(f"[caravan-agent] health endpoint listening on :{config.AGENT_PORT}/health "
          "(so peers can verify this node, not just reach it)")

    have_model = ollama_store.has_model(args.model)
    print(f"[caravan-agent] target model: {args.model} -- "
          f"{'present locally' if have_model else 'not present locally'}")

    peers = discover_peers(me["node_id"])
    print(f"[caravan-agent] discovered {len(peers)} peer(s):")
    for p in peers:
        print(f"    {p['source']:9s} {p['hostname']:30s} {p['ip']}")

    if have_model:
        role = "head"
        blob_path = ollama_store.resolve_blob_path(args.model)
        if blob_path is None:
            print(f"[caravan-agent] ERROR: manifest for {args.model} found but "
                  "GGUF blob is missing -- cannot act as head.", file=sys.stderr)
            return 1

        rpc_addrs = [f"{p['ip']}:{config.RPC_PORT}" for p in peers if p["source"] == "tailscale"]
        cmd = supervisor.build_head_command(str(blob_path), rpc_addrs)
        print(f"[caravan-agent] role: HEAD for {args.model}")
        print(f"[caravan-agent] would run: {' '.join(cmd)}")

        if args.apply:
            log_path = config.STATE_DIR / "logs" / "llama-server.log"
            print(f"[caravan-agent] --apply set, supervising now (log: {log_path})")
            supervisor.supervise(cmd, log_path)
    else:
        role = "tail"
        cmd = supervisor.build_tail_command()
        print("[caravan-agent] role: TAIL (no local model -- ready to serve compute)")
        print(f"[caravan-agent] would run: {' '.join(cmd)}")

        if args.apply:
            log_path = config.STATE_DIR / "logs" / "rpc-server.log"
            print(f"[caravan-agent] --apply set, supervising now (log: {log_path})")
            supervisor.supervise(cmd, log_path)

    if not args.apply:
        print("[caravan-agent] dry run -- pass --apply to actually spawn this.")
        if args.serve_for > 0:
            print(f"[caravan-agent] staying up for {args.serve_for:.0f}s "
                  "so peers can discover this node...")
            time.sleep(args.serve_for)

    return 0


if __name__ == "__main__":
    sys.exit(main())
