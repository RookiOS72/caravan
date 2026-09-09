"""Local Thunderbolt Bridge fast-path detection.

Finds this node's own link-local (or otherwise assigned) IPv4 address on
any Thunderbolt Bridge interface that's actually up -- these are the
addresses a peer should try first for --rpc when reachable, since a
direct wired Thunderbolt link is dramatically faster than Tailscale/WiFi
(see docs/LOG.md, "The cable arrived, tested live": 14-22x faster cold
transfer, ~35-60% faster generation, measured 2026-09-08).

Local-only, matches system_stats.py's philosophy: each node reports what
IS true about itself over its own /health endpoint; a peer decides what
to do with that (see caravan_agent.py's _select_best_address). No
speed measurement here -- interface type (Thunderbolt) is the whole
signal; see caravan_agent.py for the fixed priority order this feeds
into.
"""
import re
import subprocess

_THUNDERBOLT_PORT_RE = re.compile(r"^Thunderbolt \d+$", re.IGNORECASE)


def _thunderbolt_devices() -> list[str]:
    """Interface names (e.g. "en2") for every hardware port macOS labels
    "Thunderbolt N" -- confirmed via `networksetup -listallhardwareports`
    tonight, not guessed. Note this is the *hardware port* label, distinct
    from whatever a network *service* built on top of it is named (this
    fleet's services are labeled "EXO Thunderbolt N" from exo's own
    earlier setup -- a different namespace, not matched here)."""
    try:
        out = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return []

    devices = []
    port_name = None
    for line in out.splitlines():
        if line.startswith("Hardware Port: "):
            port_name = line[len("Hardware Port: "):].strip()
        elif line.startswith("Device: ") and port_name and _THUNDERBOLT_PORT_RE.match(port_name):
            devices.append(line[len("Device: "):].strip())
            port_name = None
    return devices


def local_fast_paths() -> list[str]:
    """This node's own IPv4 address(es) on any up Thunderbolt Bridge
    interface -- empty if no cable is connected, matching the macOS
    behavior confirmed tonight (the interface exists either way, only
    carries an address once actually linked)."""
    paths = []
    for dev in _thunderbolt_devices():
        try:
            out = subprocess.run(
                ["ifconfig", dev], capture_output=True, text=True, timeout=3, check=True,
            ).stdout
        except (subprocess.SubprocessError, OSError):
            continue
        if "status: active" not in out:
            continue
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        if m:
            paths.append(m.group(1))
    return paths
