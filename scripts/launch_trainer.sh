#!/usr/bin/env bash
# Foreground tab 2 of the chessckers fleet: the TRAINER BRIDGE. It spawns the
# chessckers engine's continuous AlphaZero trainer (chessckers_engine.
# train_continuous), feeds it the ccz1 games this server collected, and uploads
# each freshly published weights.bin back to the server (which promotes it via a
# match). Ctrl-C stops the bridge AND the trainer. Run AFTER launch_server.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

SERVER="${SERVER:-http://localhost:9830}"   # bridge runs on the server host
ENGINE_DIR="${ENGINE_DIR:-/Users/ox/AAworkspace/chessckers/engine}"
TRAINING_ID="${TRAINING_ID:-1}"
RUN_DIR="${RUN_DIR:-$(pwd)/trainer/run${TRAINING_ID}}"
PY="${ENGINE_DIR}/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$RUN_DIR"
echo "[trainer] server=$SERVER engine=$ENGINE_DIR run=$RUN_DIR py=$PY"
exec "$PY" trainer/trainer_bridge.py \
    --server "$SERVER" \
    --games-dir "$(pwd)/games/run${TRAINING_ID}" \
    --run-dir "$RUN_DIR" \
    --engine-dir "$ENGINE_DIR" \
    --training-id "$TRAINING_ID"
