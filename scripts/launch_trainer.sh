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
# MUST be v2: the akshay-chessckers-0 engine encodes 16 position planes (v2). A v1
# net has a 15-channel input conv and the engine SIGTRAPs evaluating it. TF_BLOCKS
# adds transformer blocks to the v2 trunk (0 = pure ResNet).
ARCH_VERSION="${ARCH_VERSION:-v2}"
TF_BLOCKS="${TF_BLOCKS:-0}"
PY="${ENGINE_DIR}/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$RUN_DIR"
echo "[trainer] server=$SERVER engine=$ENGINE_DIR run=$RUN_DIR arch=$ARCH_VERSION tf=$TF_BLOCKS py=$PY"
exec "$PY" trainer/trainer_bridge.py \
    --server "$SERVER" \
    --games-dir "$(pwd)/games/run${TRAINING_ID}" \
    --run-dir "$RUN_DIR" \
    --engine-dir "$ENGINE_DIR" \
    --training-id "$TRAINING_ID" \
    --arch-version "$ARCH_VERSION" \
    --tf-blocks "$TF_BLOCKS"
