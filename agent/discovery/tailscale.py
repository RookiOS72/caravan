"""Tailscale-based peer discovery.

Preferred over LAN IPs per docs/ARCHITECTURE.md: tonight's incident found
that connecting to a peer's LAN IP could fail with `EHOSTUNREACH` from
certain process contexts even when `ping`/`nc` to the same IP succeeded,
while the same peer's Tailscale IP worked reliably every time (see
docs/LOG.md, "The retry cascade, root-caused" and the full-rebrand entry).
Root cause not fully explained; the empirical fix is what this module
exists to apply automatically instead of by hand.

Shells out to the `tailscale` CLI rather than using Tailscale's own
Go client library, matching this project's dependency-free-Python
approach so far (see proxy/slot_pin_proxy.py's docstring).
"""
import json
import shutil
import subprocess
from dataclasses import dataclass

# Homebrew-installed Tailscale.app puts the CLI here; not always on PATH.
# Fall back to PATH lookup for other install methods.
_CANDIDATE_BINARIES = [
    "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
    "tailscale",
]


@dataclass
class TailscalePeer:
    hostname: str
    tailscale_ip: str
    online: bool


def _find_binary() -> str | None:
    for candidate in _CANDIDATE_BINARIES:
        if candidate.startswith("/"):
            if shutil.which(candidate) or __import__("os").path.exists(candidate):
                return candidate
        elif shutil.which(candidate):
            return candidate
    return None


def is_available() -> bool:
    return _find_binary() is not None


def get_peers(timeout: float = 5.0) -> list[TailscalePeer]:
    """Returns every peer in this node's tailnet, online or not -- callers
    should filter on `.online` themselves. Empty list if Tailscale isn't
    installed/running rather than raising, since it's an optional
    discovery path (see ARCHITECTURE.md's hybrid design)."""
    binary = _find_binary()
    if binary is None:
        return []

    try:
        result = subprocess.run(
            [binary, "status", "--json"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        data = json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
        # Logged, not silent: this exact path has a real known failure mode
        # under launchd specifically -- the GUI-app-bundled `tailscale`
        # binary's CLI shim talks to the running Tailscale.app over local
        # IPC, which can fail from a launchd LaunchAgent context even
        # though the same command works fine from an interactive shell
        # (confirmed 2026-09-07: `Tailscale.CLIError error 3` on stdout,
        # exit code 0, empty JSON -- see docs/LOG.md, "node-a agent
        # cutover: Tailscale discovery fails under launchd"). Not fixed
        # here; this is a discovery-source failure, not a crash -- the
        # caller falls back to whatever mDNS finds, or single-node.
        print(f"[caravan-agent] tailscale.get_peers() failed ({exc!r}) -- "
              "falling back to mDNS/single-node")
        return []

    peers = []
    for peer in data.get("Peer", {}).values():
        ipv4 = next(
            (ip for ip in peer.get("TailscaleIPs", []) if "." in ip), None
        )
        if ipv4 is None:
            continue
        peers.append(
            TailscalePeer(
                hostname=peer.get("HostName", ""),
                tailscale_ip=ipv4,
                online=bool(peer.get("Online", False)),
            )
        )
    return peers
