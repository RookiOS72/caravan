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
import json
import sys
import time
from pathlib import Path

import config
import health_server
import identity
import ollama_store
import supervisor
from discovery import mdns, tailscale


def _agent_health(ip: str, timeout: float = 1.5) -> dict | None:
    """Full parsed /health response from `ip`:AGENT_PORT, or None if it
    doesn't answer -- the gate that stops "some other machine happens to
    be reachable" from being trusted as a real tail. Without this,
    Tailscale discovery in particular will find every peer in the
    tailnet, Caravan or not -- confirmed directly: a dry run tonight
    found an unrelated tailnet machine (prf-hays-mgmt) and would have
    wired it into --rpc as if it were a tail. See health_server.py.

    Returns the full body (not just True/False) so callers can read a
    peer's self-reported `fast_paths` (Thunderbolt addresses -- see
    discovery/thunderbolt.py) without a second round trip.
    """
    try:
        conn = http.client.HTTPConnection(ip, config.AGENT_PORT, timeout=timeout)
        conn.request("GET", "/health")
        resp = conn.getresponse()
        if resp.status != 200:
            resp.read()
            conn.close()
            return None
        body = json.loads(resp.read())
        conn.close()
        return body
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[caravan-agent] _agent_health({ip!r}) failed: {exc!r}")
        return None


def is_agent_alive(ip: str, timeout: float = 1.5) -> bool:
    """True only if a Caravan agent's health endpoint actually answers.
    Thin wrapper around _agent_health for call sites that just need the
    liveness gate, not the response body (e.g. probing a candidate
    fast-path address -- see _select_best_address)."""
    return _agent_health(ip, timeout=timeout) is not None


def discover_peers(agent_id: str, self_hostname: str | None = None) -> list[dict]:
    """Merges Tailscale + mDNS sightings into one peer list, preferring
    the Tailscale IP for a peer found both ways (see ARCHITECTURE.md),
    and keeping only peers that actually answer as a Caravan agent (see
    is_agent_alive above) -- a peer that's merely *reachable* isn't
    necessarily *ours*.

    `self_hostname`: this node's own hostname, so its own mDNS
    advertisement doesn't get discovered as if it were a peer -- a real
    bug hit in testing (2026-09-07, see docs/LOG.md "mDNS fallback"):
    once this agent started advertising itself (see main()), its own
    `dns-sd -B` browse call found that same advertisement and the head
    command tried to add itself as its own --rpc target. Tailscale's
    `status --json` already excludes self by construction (its "Peer"
    list never includes the local node), so this filter only matters for
    the mDNS path.

    Reconciling an mDNS sighting with a Tailscale one by node identity
    isn't implemented yet -- this agent doesn't publish its own node_id
    in an mDNS TXT record yet, so this dedupes by normalized hostname
    (see _rpc_addrs_from_peers) rather than true node identity. Good
    enough for today's 2-node LAN.

    mDNS is not just supplemental info: as of 2026-09-07, Tailscale
    discovery has a real known failure mode under launchd specifically
    (the App-bundled CLI's IPC to the running GUI app fails from a
    LaunchAgent context -- see docs/LOG.md, "Tailscale discovery fails
    under launchd"), so mDNS is what actually makes head-role discovery
    work in practice right now, not tailscale.get_peers().
    """
    peers = []

    for peer in tailscale.get_peers():
        if not peer.online:
            continue
        health = _agent_health(peer.tailscale_ip)
        if health is None:
            continue
        peers.append({
            "source": "tailscale",
            "hostname": peer.hostname,
            "ip": peer.tailscale_ip,
            "fast_paths": health.get("fast_paths", []),
        })

    for peer in mdns.discover_peers():
        if self_hostname is not None and _normalize_hostname(peer.hostname) == _normalize_hostname(self_hostname):
            continue
        health = _agent_health(peer.hostname)
        if health is None:
            continue
        peers.append({
            "source": "mdns",
            "hostname": peer.hostname,
            "fast_paths": health.get("fast_paths", []),
            "ip": peer.hostname,  # resolved hostname, not yet a bare IP
            "port": peer.port,
        })

    return peers


def _normalize_hostname(hostname: str) -> str:
    """Best-effort node-identity match across sources: strips a trailing
    ".local"/domain suffix and lowercases, so "prf-hays-exo02" (Tailscale)
    and "prf-hays-exo02.local" (mDNS) are recognized as the same node.
    Not true identity reconciliation (no shared node_id yet -- see
    discover_peers) but good enough to avoid double-counting the same
    peer as two --rpc targets when both sources find it.
    """
    return hostname.split(".")[0].lower()


def _select_best_address(peer: dict) -> str:
    """Picks the fastest reachable address for one peer: tries each of
    its self-reported Thunderbolt fast_paths (see discovery/thunderbolt.py
    and health_server.py) in order, falling back to whatever address
    discover_peers found it through (Tailscale preferred there, mDNS
    otherwise) if none of them answer.

    Reachability, not speed, is what's tested -- interface type
    (Thunderbolt > Tailscale > mDNS/LAN) is the whole ranking signal, no
    bandwidth measurement. Reuses is_agent_alive (the same already-proven
    liveness check used to validate the peer in the first place) against
    each candidate's AGENT_PORT. This doubles as automatic fallback if a
    Thunderbolt cable is ever unplugged: the candidate simply stops
    answering and the next one in priority order gets used instead --
    see docs/LOG.md, "auto-select fastest available network path", for
    why this was built as one feature rather than two.
    """
    for candidate in peer.get("fast_paths", []):
        if is_agent_alive(candidate):
            return candidate
    return peer["ip"]


def _rpc_addrs_from_peers(peers: list[dict]) -> list[str]:
    """Builds the --rpc address list, Tailscale preferred over mDNS per
    ARCHITECTURE.md when neither has a reachable Thunderbolt fast-path
    (mDNS is the fallback for today's Tailscale-CLI-under-launchd
    failure -- see discover_peers's docstring). Always uses
    config.RPC_PORT regardless of source; a peer dict's own "port" field
    (when present) is that peer's *agent* health port, not its RPC port.
    """
    by_node: dict[str, dict] = {}
    for p in peers:
        key = _normalize_hostname(p["hostname"])
        if key not in by_node or p["source"] == "tailscale":
            by_node[key] = p
    return [f"{_select_best_address(p)}:{config.RPC_PORT}" for p in by_node.values()]


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

    # Advertise so *other* nodes' mdns.discover_peers() can find this one --
    # discover_peers() below only browses, it never advertised this node
    # itself (a real gap: mDNS browsing found nothing on any node until
    # this was added, since nothing was ever registered to find -- see
    # docs/LOG.md, "mDNS fallback"). `advertiser` must be terminated on
    # shutdown or dns-sd leaves this node's registration behind after the
    # process exits (see mdns.advertise_start's docstring); the `finally`
    # below covers both the --apply (supervise, blocks) and dry-run paths.
    advertiser = mdns.advertise_start(me["hostname"], config.AGENT_PORT)
    print(f"[caravan-agent] advertising via mDNS as {me['hostname']!r}")

    have_model = ollama_store.has_model(args.model)
    print(f"[caravan-agent] target model: {args.model} -- "
          f"{'present locally' if have_model else 'not present locally'}")

    peers = discover_peers(me["node_id"], self_hostname=me["hostname"])
    print(f"[caravan-agent] discovered {len(peers)} peer(s):")
    for p in peers:
        print(f"    {p['source']:9s} {p['hostname']:30s} {p['ip']}")

    try:
        if have_model:
            role = "head"
            blob_path = ollama_store.resolve_blob_path(args.model)
            if blob_path is None:
                print(f"[caravan-agent] ERROR: manifest for {args.model} found but "
                      "GGUF blob is missing -- cannot act as head.", file=sys.stderr)
                return 1

            rpc_addrs = _rpc_addrs_from_peers(peers)
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
    finally:
        advertiser.terminate()


if __name__ == "__main__":
    sys.exit(main())
