#!/usr/bin/env bash
# Restart node-b's rpc-server safely, then bring llama-server back up.
#
# rpc-server must never be restarted while llama-server is running: it's
# stateless (holds node-b's half of the model only in that process's own
# RAM) and llama-server keeps a live RPC session against it for its
# entire runtime, so any rpc-server restart severs that connection and
# crashes llama-server (ggml_abort). See docs/LOG.md, "Self-inflicted
# crash: restarting node-b's rpc-server after llama-server was already
# loaded". This script does the only safe order: stop llama-server,
# restart rpc-server, start llama-server -- instead of touching
# rpc-server directly.
#
# With node-b's rpc-server cache warm (docs/LOG.md, "Issue #3: solved
# natively"), the llama-server restart this triggers is typically ~50-60s,
# not the 7-11 minutes a cold cache/first-ever run takes.
set -euo pipefail

NODE_B_HOST="${CARAVAN_NODE_B_HOST:-192.168.1.39}"
NODE_B_USER="${CARAVAN_NODE_B_USER:-brenden}"
REPO_DIR="${CARAVAN_REPO_DIR:-$HOME/dev/caravan}"
UID_NUM="$(id -u)"
LLAMA_HEALTH_URL="http://127.0.0.1:8080/health"
LLAMA_SLOTS_URL="http://127.0.0.1:8080/slots"

log() { echo "[restart-rpc-server] $*"; }

wait_for_process_exit() {
  local pattern="$1" timeout_s="${2:-20}"
  for ((i = 0; i < timeout_s; i++)); do
    pgrep -f "$pattern" >/dev/null || return 0
    sleep 1
  done
  return 1
}

slots_idle() {
  local slots
  slots="$(curl -sf "$LLAMA_SLOTS_URL" 2>/dev/null)" || {
    log "llama-server not reachable -- nothing in flight, safe to proceed"
    return 0
  }
  echo "$slots" | python3 -c "
import json, sys
d = json.load(sys.stdin)
sys.exit(1 if any(s.get('is_processing') for s in d) else 0)
"
}

if ! slots_idle; then
  if [[ "${1:-}" != "--force" ]]; then
    log "ERROR: llama-server has an in-flight request. Re-run with --force to interrupt it, or wait until it's idle."
    exit 1
  fi
  log "WARNING: --force given, restarting with an in-flight request -- it will be cancelled."
fi

log "1/4 stopping llama-server (node-a)"
launchctl bootout "gui/${UID_NUM}/com.caravan.llama-server" 2>/dev/null || true
if ! wait_for_process_exit "llama-server -m" 20; then
  log "ERROR: llama-server didn't stop within 20s"
  exit 1
fi
log "    stopped"

log "2/4 restarting rpc-server (node-b)"
ssh "${NODE_B_USER}@${NODE_B_HOST}" "
  launchctl bootout gui/\$(id -u)/com.caravan.rpc-server 2>/dev/null || true
  for i in \$(seq 1 20); do
    pgrep -f ggml-rpc-server >/dev/null || break
    sleep 1
  done
  sleep 1
  launchctl bootstrap gui/\$(id -u) '${REPO_DIR}/launchd/com.caravan.rpc-server.plist'
  sleep 1
  pgrep -f ggml-rpc-server >/dev/null || { echo 'ERROR: rpc-server did not start' >&2; exit 1; }
"
log "    rpc-server running"

log "3/4 starting llama-server (node-a)"
start_ts=$(date +%s)
launchctl bootstrap "gui/${UID_NUM}" "${REPO_DIR}/launchd/com.caravan.llama-server.plist"

log "4/4 waiting for llama-server to report healthy"
while true; do
  if curl -sf "$LLAMA_HEALTH_URL" 2>/dev/null | grep -q '"status":"ok"'; then
    break
  fi
  sleep 3
done
elapsed=$(($(date +%s) - start_ts))
log "ready after ${elapsed}s"
