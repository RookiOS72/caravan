"""The health-check surface every Caravan agent exposes, so other agents'
discovery passes can tell "this Tailscale/mDNS peer is actually running
Caravan" apart from "this is just some other machine on the same LAN or
tailnet" -- see docs/ARCHITECTURE.md's open question on this, and the
caravan_agent.py dry-run output on 2026-09-06 that (correctly, if
uselessly) discovered an unrelated tailnet peer and would have wired it
into `--rpc` had this gate not existed.

Deliberately tiny: one endpoint, no auth (matches the existing
slot-pin-proxy's loopback-only trust model, though this binds all
interfaces since peers need to reach it -- LAN/tailnet-only by design,
same posture as rpc-server itself, see launchd/README.md's security
note).

Also carries this node's own resource stats (load average, memory
pressure, relevant disk headroom -- see system_stats.py) alongside the
liveness check. One endpoint doing both rather than a second one,
since anything polling for "is this peer alive" benefits from getting
"and how loaded is it" in the same round trip for free.

2026-09-06: first real cross-node test (see docs/LOG.md) found this
hanging for ~35s on startup on node-b specifically. Root cause:
`http.server.HTTPServer.server_bind()` calls `socket.getfqdn(host)` to
set `self.server_name`, and `getfqdn('0.0.0.0')` does a reverse-DNS-style
lookup that's slow/near-broken on that machine's network config for
reasons not otherwise investigated -- confirmed in isolation, unrelated
to anything else in this file. `_Server` below skips that lookup
entirely; nothing here actually uses `server_name`/`server_port` for
anything, so there's no downside to not resolving them.
"""
import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config
import system_stats


class _Server(ThreadingHTTPServer):
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        # Skip HTTPServer's socket.getfqdn() reverse-DNS lookup -- see
        # module docstring. Bare IP is all we ever need here.
        self.server_name = self.server_address[0]
        self.server_port = self.server_address[1]


class _HealthHandler(BaseHTTPRequestHandler):
    node_id = "unknown"

    def log_message(self, fmt, *args):
        pass  # quiet -- this gets polled frequently by peers

    def do_GET(self):
        if self.path != "/health":
            self.send_response(404)
            self.end_headers()
            return
        try:
            stats = system_stats.gather([config.SLOT_SAVE_DIR, config.RPC_CACHE_DIR])
        except Exception as exc:  # noqa: BLE001 -- stats are a bonus, never fail liveness over them
            stats = {"error": str(exc)}
        body = json.dumps({
            "status": "ok",
            "node_id": self.node_id,
            "stats": stats,
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start(node_id: str) -> threading.Thread:
    """Starts the health endpoint on config.AGENT_PORT in a background
    daemon thread and returns it. Dies with the process -- nothing to
    explicitly stop."""
    handler = type("_BoundHealthHandler", (_HealthHandler,), {"node_id": node_id})
    server = _Server(("0.0.0.0", config.AGENT_PORT), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
