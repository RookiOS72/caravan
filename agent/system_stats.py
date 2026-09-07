"""Local resource stats for this node, exposed via health_server.py's
/health endpoint -- the monitoring signal from GitHub issue #7's "what
else should peers tell each other" brainstorm.

Deliberately local-only: each node reports what's true about itself
using stdlib calls and `vm_stat`, not a hand-rolled `ggml-rpc` binary
protocol client to ask a peer about its own state remotely. GPU memory
specifically isn't reported here for that reason -- `ggml-rpc` does have
a real RPC_CMD_GET_DEVICE_MEMORY command that would answer it, but
speaking that wire protocol from Python is a separate, real chunk of
work, not something to bolt on alongside a stdlib-only stats module. On
this Apple Silicon hardware system memory pressure (below) is already a
reasonable proxy anyway, since Metal draws from the same unified memory
pool as everything else -- see docs/LOG.md's kv_unified=false entries,
which used exactly this (`vm_stat`'s "Pages free") to judge headroom by
hand all night. This module just automates that.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path


def _vm_stat() -> dict[str, int]:
    """Parses `vm_stat`'s page-count lines into byte counts, using the
    actual page size vm_stat reports rather than assuming one -- Apple
    Silicon uses 16384-byte pages, Intel Macs use 4096, and hardcoding
    the wrong one silently misreports memory by 4x."""
    out = subprocess.run(
        ["vm_stat"], capture_output=True, text=True, timeout=5
    ).stdout
    page_size_match = re.search(r"page size of (\d+) bytes", out)
    page_size = int(page_size_match.group(1)) if page_size_match else 4096

    stats: dict[str, int] = {}
    for line in out.splitlines()[1:]:
        m = re.match(r"^(.+?):\s+(\d+)\.$", line)
        if m:
            stats[m.group(1).strip()] = int(m.group(2)) * page_size
    return stats


def _disk_usage(paths: list[Path]) -> dict[str, dict[str, int]]:
    """Only reports a path if it actually exists -- e.g. a head node
    won't have RPC_CACHE_DIR, a tail node won't have SLOT_SAVE_DIR.
    Missing paths are silently skipped, not an error."""
    result = {}
    for p in paths:
        try:
            usage = shutil.disk_usage(p)
        except OSError:
            continue
        result[str(p)] = {"free_bytes": usage.free, "total_bytes": usage.total}
    return result


def gather(disk_paths: list[Path] | None = None) -> dict:
    """Returns a JSON-serializable snapshot of this node's own resource
    state. Cheap (one subprocess call, a couple of syscalls) -- safe to
    call on every /health request rather than caching/polling on a
    separate timer."""
    load1, load5, load15 = os.getloadavg()
    vm = _vm_stat()

    return {
        "load_avg": {"1m": load1, "5m": load5, "15m": load15},
        "memory": {
            "free_bytes": vm.get("Pages free", 0) + vm.get("Pages speculative", 0),
            "active_bytes": vm.get("Pages active", 0),
            "inactive_bytes": vm.get("Pages inactive", 0),
            "wired_bytes": vm.get("Pages wired down", 0),
        },
        "disk": _disk_usage(disk_paths or []),
    }
