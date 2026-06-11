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
# Warm-resume by default: if a published net exists in the run-dir, restart FROM it
# instead of random init. train_continuous resumes warm (no cold min-buffer wait),
# including the SGD momentum + LR-schedule step counter from the replay snapshot.
# Set BASE="" explicitly to force a fresh random init.
BASE="${BASE-$([ -f "$RUN_DIR/weights.pt" ] && echo "$RUN_DIR/weights.pt" || true)}"
# MUST be v2: the akshay-chessckers-0 engine encodes 16 position planes (v2). A v1
# net has a 15-channel input conv and the engine SIGTRAPs evaluating it. TF_BLOCKS
# adds transformer blocks to the v2 trunk (0 = pure ResNet).
# V4 = SE-ResNet (gather head + Squeeze-Excitation blocks, tf=0). The default net.
# Override ARCH_VERSION=v2 + TF_BLOCKS=7 for the transformer net, or v2/tf=0 for plain ResNet.
ARCH_VERSION="${ARCH_VERSION:-v4}"
TF_BLOCKS="${TF_BLOCKS:-0}"
SE_RATIO="${SE_RATIO:-8}"
N_BLOCKS="${N_BLOCKS:-5}"      # V4_e8d8: c48 b5 ~364k (3.5x the c16 b1 ~104k tiny run). Scale up by raising these two (prod was c96 b8 ~1.58M).
C_FILTERS="${C_FILTERS:-48}"

# --- lc0-style training cadence (Phases 1-2). Publishing is gated on TRAINING
#     PROGRESS, not wall-clock: a new net every PUBLISH_GAMES of fresh self-play
#     (so the server gates ~once per generation instead of every 45s, which was
#     starving self-play with gating matches). Data is a sliding window of the
#     last WINDOW_GAMES games; the published net is an EMA of the weights. ---
MIN_BUFFER="${MIN_BUFFER:-200}"         # start SGD once the buffer reaches this (was 1000; lowered for a faster cold start)
BUFFER_CAP="${BUFFER_CAP:-500000}"      # hard RAM position cap (was code-default 50000). MUST exceed
                                        # WINDOW_GAMES x mean-plies, else the position cap evicts before
                                        # the game-count window ramp can grow (50k only held ~500 games).
# Replay buffer persists to <run-dir>/replay_buffer.pkl on every CLEAN shutdown
# (always on) so a restart to tweak a hyperparameter resumes the exact window
# with no cold rebuild. Set >0 to ALSO snapshot every N s for crash safety (the
# buffer can be large, so periodic writes cost I/O; 0 = shutdown-only).
BUFFER_SNAPSHOT_SECONDS="${BUFFER_SNAPSHOT_SECONDS:-0}"
PUBLISH_GAMES="${PUBLISH_GAMES:-250}"   # publish a net per this many ingested games (arenas removed -> frequent publish is free; AZ-final-style continuous deploy)
PUBLISH_STEPS="${PUBLISH_STEPS:-0}"     # OR per this many SGD steps (0 = off)
PUBLISH_SECONDS="${PUBLISH_SECONDS:-0}" # OR time floor (0 = off; progress-gated)
# Replay window in GAMES — lc0/KataGo GROWING window: starts at WINDOW_GAMES_MIN
# (narrow, so early near-random games evict fast and the net escapes random play)
# and ramps sublinearly up to WINDOW_GAMES (the ceiling: wider/stabler once the
# policy settles). Set WINDOW_GAMES_MIN=0 for a fixed window (legacy behavior).
WINDOW_GAMES="${WINDOW_GAMES:-4000}"        # ramp CEILING (max window; was 2000 -> 4000 for a bigger replay buffer)
WINDOW_GAMES_MIN="${WINDOW_GAMES_MIN:-400}" # ramp FLOOR (start window; 0 = fixed, no ramp)
WINDOW_RAMP_ALPHA="${WINDOW_RAMP_ALPHA:-0.75}"  # ramp exponent (KataGo default; lower = slower/wider ramp)
REPLAY_FACTOR="${REPLAY_FACTOR:-40}"    # max samples = this x positions-ingested. 40 (was code-default 8) since the trainer is generation-bound (idle ~88% waiting on self-play); raise to take more SGD steps per game
VALUE_DISCOUNT="${VALUE_DISCOUNT:-1.0}"   # per-ply WDL discount gamma. 1.0 = OFF (pure WDL): "mate faster" now comes from the moves-left HEAD's Q-gated search effect (engine has_mlh), not from discounting the win itself — so a faster-but-riskier line can't beat a slower-certain win (the discount's failure mode). <1 still works (0.99 = ~0.6 win-mass at 50 plies) but reintroduces that risk; prefer Q_RATIO for the early-game variance the discount used to mask.
VALUE_Q_RATIO="${VALUE_Q_RATIO:-0.5}"     # blend the search value q into the value target: (1-r)*z + r*q. The lc0-idiomatic variance reducer (and the companion to discount=1.0: softens overconfident early one-hot z without distorting the win). Default 0.5 (50/50 z/q); set 0.0 to disable. The engine emits search_wdl, so this is ready.
LR="${LR:-0.02}"                        # base LR (SGD+Nesterov; ~20x the old Adam 1e-3)
LR_WARMUP_STEPS="${LR_WARMUP_STEPS:-0}"   # 0 = no warmup (flat from step 0). Set >0 to experiment with a linear ramp.
LR_DECAY_STEPS="${LR_DECAY_STEPS:-0}"   # 0 = no step schedule / constant LR. Set >0 (+LR_GAMMA) to experiment with stepped decay.
LR_GAMMA="${LR_GAMMA:-0.5}"
EMA_DECAY="${EMA_DECAY:-0.999}"         # publish EMA of weights (0 = raw)
BATCH_SIZE="${BATCH_SIZE:-1024}"        # SGD minibatch (lc0 uses 1024-4096; was bridge-default 256). Larger -> smoother gradients
PY="${ENGINE_DIR}/.venv/bin/python"; [ -x "$PY" ] || PY="$(command -v python3)"

mkdir -p "$RUN_DIR"
echo "[trainer] server=$SERVER engine=$ENGINE_DIR run=$RUN_DIR arch=$ARCH_VERSION tf=$TF_BLOCKS se=$SE_RATIO c=$C_FILTERS b=$N_BLOCKS"
echo "[trainer] publish=[${PUBLISH_GAMES}g/${PUBLISH_STEPS}st/${PUBLISH_SECONDS}s] window=${WINDOW_GAMES_MIN}->${WINDOW_GAMES}g@a${WINDOW_RAMP_ALPHA} replay_factor=$REPLAY_FACTOR value_discount=$VALUE_DISCOUNT q_ratio=$VALUE_Q_RATIO min_buffer=$MIN_BUFFER buffer_cap=$BUFFER_CAP batch=$BATCH_SIZE ema=$EMA_DECAY lr=$LR warmup=$LR_WARMUP_STEPS lr_decay=${LR_DECAY_STEPS}@${LR_GAMMA}"
echo "[trainer] base=${BASE:-<random init>}"
exec "$PY" trainer/trainer_bridge.py \
    --server "$SERVER" \
    --base "$BASE" \
    --games-dir "$(pwd)/games/run${TRAINING_ID}" \
    --run-dir "$RUN_DIR" \
    --engine-dir "$ENGINE_DIR" \
    --training-id "$TRAINING_ID" \
    --arch-version "$ARCH_VERSION" \
    --tf-blocks "$TF_BLOCKS" \
    --se-ratio "$SE_RATIO" \
    --c-filters "$C_FILTERS" \
    --n-blocks "$N_BLOCKS" \
    --min-buffer "$MIN_BUFFER" \
    --buffer-cap "$BUFFER_CAP" \
    --batch-size "$BATCH_SIZE" \
    --buffer-snapshot-seconds "$BUFFER_SNAPSHOT_SECONDS" \
    --publish-games "$PUBLISH_GAMES" \
    --publish-steps "$PUBLISH_STEPS" \
    --publish-seconds "$PUBLISH_SECONDS" \
    --window-games "$WINDOW_GAMES" \
    --window-games-min "$WINDOW_GAMES_MIN" \
    --window-ramp-alpha "$WINDOW_RAMP_ALPHA" \
    --replay-factor "$REPLAY_FACTOR" \
    --value-discount "$VALUE_DISCOUNT" \
    --value-q-ratio "$VALUE_Q_RATIO" \
    --lr "$LR" \
    --lr-warmup-steps "$LR_WARMUP_STEPS" \
    --lr-decay-steps "$LR_DECAY_STEPS" \
    --lr-gamma "$LR_GAMMA" \
    --ema-decay "$EMA_DECAY"
