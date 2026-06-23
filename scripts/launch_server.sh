#!/usr/bin/env bash
# Foreground tab 1 of the chessckers fleet: build + run the dispatch/training
# SERVER, bound to the Tailscale tailnet. Bootstraps the SQLite DB + run #1 on
# first start (idempotent). Ctrl-C stops the server; logs stream in-tab.
#
# The server reads serverconfig.json / templates/ / public/ relative to CWD, so
# this script runs it from the repo root.
set -euo pipefail
cd "$(dirname "$0")/.."

# Guard: fleet runs on the cloud GPU box, not on macOS.
if [ "$(uname -s)" = Darwin ] && [ -z "${ON_BOX:-}" ]; then
  echo "[guard] This script runs on the Vast.ai GPU box, not locally." >&2
  echo "[guard] Use: python engine/scripts/cc.py <command>" >&2
  echo "[guard] Or: cc box / cc fresh-run / cc restart-trainer / cc status / cc games" >&2
  exit 1
fi

# Experiment name for the bootstrapped run (TrainingRun.Description; shown in the
# dashboard). Re-bootstrap after reset_fleet.sh picks this up; no-op if a run exists.
RUN_NAME="${RUN_NAME:-V4_e8d8}"

echo "[server] building..."
CGO_ENABLED=1 go build -o cc-server .
CGO_ENABLED=1 go build -o cc-bootstrap ./cmd/bootstrap
RUN_NAME="$RUN_NAME" ./cc-bootstrap   # schema + run #1 named "$RUN_NAME" (+ train/match params); no-op if already present

ts_name="$(hostname -s 2>/dev/null || hostname)"
ts_ip="$(tailscale ip -4 2>/dev/null | head -1 || echo '?')"
echo "[server] listening on :9830"
echo "[server] tailnet URL for clients:  http://${ts_name}:9830   (ip ${ts_ip})"
echo "[server] e.g.  SERVER=http://${ts_name}:9830 scripts/launch_client.sh   (in lczero-client)"
exec env GIN_MODE=release ./cc-server
