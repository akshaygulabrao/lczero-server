#!/usr/bin/env bash
# Provision a fresh vast.ai GPU box to host the chessckers SERVER + TRAINER.
# The two are co-located BY DESIGN: the trainer bridge reads the server's local
# games/ dir and uploads nets to localhost (see trainer/trainer_bridge.py), so
# they must share a machine. One-time per box; afterwards run_server_vast.sh
# starts/restarts the fleet on it.
#
# What it does (run FROM the Mac):
#   1. installs the toolchain on the box: build-essential (gcc for cgo-sqlite),
#      Go $GO_VERSION (the server's go.mod needs >=1.25; apt's Go is too old),
#      uv (engine venv), tmux/rsync.
#   2. rsyncs the server repo (code only) + builds cc-server/cc-bootstrap on the
#      box with CGO_ENABLED=1 (mattn/go-sqlite3 needs cgo -> it can't be
#      cross-compiled cpu-off the way the Go *client* is).
#   3. rsyncs the engine repo + `uv sync` (pulls the CUDA torch wheel on linux;
#      train_continuous's pick_device("auto") then uses the GPU automatically).
#   4. SEED_STATE=true (default): rsyncs the live run up -- chessckers.db*,
#      networks/, trainer/run1/ -- so train_continuous RESUMES the V4_e8d8 run
#      warm (weights.pt + replay snapshot + SGD momentum/LR clock). REQUIRES the
#      local fleet be STOPPED first: the replay snapshot is written on the
#      trainer's clean shutdown, so seeding a half-written buffer loses the
#      window. Aborts if the local fleet is still running.
#
# Networking (public-port mode): cc-server binds 0.0.0.0:$SERVER_PORT in the
# container -- default 10100, one of the ports vast's stock template ALREADY opens
# (`-p 10100:10100`, and unclaimed by PORTAL_CONFIG), so NO instance recreate is
# needed. This script rewrites serverconfig.json's webserver.address to :$SERVER_PORT
# ON THE BOX ONLY (the Mac's config stays :9830). vast maps it to a random external
# port; clients use http://<public-ip>:<external-port>. Override SERVER_PORT=10200 etc.
#
# Low-downtime flow (recommended) -- pre-warm the slow build while self-play runs,
# then stop + seed:
#   SEED_STATE=false VAST_HOST=.. VAST_PORT=.. scripts/provision_server_vast.sh  # builds; fleet stays up
#   # ...stop the local fleet cleanly (Ctrl-C the trainer tab first)...
#   VAST_HOST=.. VAST_PORT=.. scripts/provision_server_vast.sh                   # fast re-sync + seed
#   VAST_HOST=.. VAST_PORT=.. scripts/run_server_vast.sh
#
# Simple flow: stop the local fleet, then run once (SEED_STATE defaults true).
#
# Usage:
#   VAST_HOST=sshN.vast.ai VAST_PORT=23456 scripts/provision_server_vast.sh
#   SEED_STATE=false ...   # skip the state seed (start the run fresh on the box)
#   SHIP_GAMES=true  ...   # also ship games/ (archival; not needed to continue)
set -euo pipefail
cd "$(dirname "$0")/.."
SERVER_SRC="$(pwd)"
ENGINE_SRC="${ENGINE_SRC:-$(cd .. && pwd)/chessckers/engine}"  # engine is nested in chessckers/; fleet repos are its siblings

VAST_HOST="${VAST_HOST:?set VAST_HOST (e.g. sshN.vast.ai) -- get it from: vastai ssh-url <id>}"
VAST_PORT="${VAST_PORT:?set VAST_PORT (the ssh port from vastai ssh-url <id>)}"
VAST_USER="${VAST_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/workspace/chessckers}"
GO_VERSION="${GO_VERSION:-1.25.0}"
SEED_STATE="${SEED_STATE:-true}"
SHIP_GAMES="${SHIP_GAMES:-false}"
SERVER_PORT="${SERVER_PORT:-10100}"   # bind cc-server to an already-open vast port (default 10100)

SSHO="-p $VAST_PORT -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"
ssh_box() { ssh $SSHO "$VAST_USER@$VAST_HOST" "$@"; }
rsync_box() { rsync -az -e "ssh $SSHO" "$@"; }

[ -d "$ENGINE_SRC" ] || { echo "[provision] engine source not found: $ENGINE_SRC" >&2; exit 1; }

# 0. State-seed safety: the local fleet must be cleanly stopped first so the DB
#    and trainer/run1/replay_buffer.pkl are consistent (the snapshot is written
#    on clean shutdown). Abort if anything is still running locally.
if [ "$SEED_STATE" = "true" ]; then
  if pgrep -fl 'cc-server|trainer_bridge|train_continuous' >/dev/null 2>&1; then
    echo "[provision] LOCAL fleet is still running -- stop it cleanly first so the DB +" >&2
    echo "            replay snapshot are consistent, then re-run. (Ctrl-C the trainer tab;" >&2
    echo "            it writes trainer/run1/replay_buffer.pkl on clean shutdown.)" >&2
    echo "            Or pass SEED_STATE=false to build now and start the run fresh on the box." >&2
    exit 1
  fi
fi

echo "[provision] box=$VAST_USER@$VAST_HOST:$VAST_PORT  remote=$REMOTE_DIR  go=$GO_VERSION seed=$SEED_STATE"
ssh_box 'nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null || echo "[provision] WARN: no nvidia-smi / GPU on the box (trainer will fall back to CPU)"'

# 1. Toolchain.
echo "[provision] (1/5) installing toolchain (build-essential, Go $GO_VERSION, uv, tmux)..."
ssh_box "GO_VERSION='$GO_VERSION' REMOTE_DIR='$REMOTE_DIR' bash -s" <<'REMOTE'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential rsync tmux curl ca-certificates
# Go: the server's go.mod needs >=1.25 (apt's Go is too old) -> official tarball.
if ! /usr/local/go/bin/go version 2>/dev/null | grep -q "go${GO_VERSION} "; then
  echo "[provision]   installing Go ${GO_VERSION}..."
  curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" -o /tmp/go.tgz
  rm -rf /usr/local/go && tar -C /usr/local -xzf /tmp/go.tgz && rm -f /tmp/go.tgz
fi
/usr/local/go/bin/go version
# uv for the engine venv.
if ! command -v uv >/dev/null 2>&1 && [ ! -x "$HOME/.local/bin/uv" ]; then
  echo "[provision]   installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
mkdir -p "$REMOTE_DIR"
REMOTE

# 2. Ship server code + build on the box (CGO for sqlite).
echo "[provision] (2/5) rsync server code + bind :$SERVER_PORT + build cc-server (CGO sqlite)..."
rsync_box --delete \
  --exclude '.git/' --exclude 'cc-server' --exclude 'cc-bootstrap' \
  --exclude 'networks/' --exclude 'games/' --exclude 'pgns/' \
  --exclude 'trainer/run*/' --exclude 'chessckers.db*' \
  "$SERVER_SRC/" "$VAST_USER@$VAST_HOST:$REMOTE_DIR/lczero-server/"
# Rewrite the bind port ON THE BOX (the rsync just wrote the Mac's :9830). serverconfig.json
# has :9830 exactly once (webserver.address), so the substitution is unambiguous.
ssh_box "cd '$REMOTE_DIR/lczero-server' && \
  sed -i 's|:9830|:$SERVER_PORT|g' serverconfig.json && \
  CGO_ENABLED=1 PATH=/usr/local/go/bin:\$PATH go build -o cc-server . && \
  CGO_ENABLED=1 PATH=/usr/local/go/bin:\$PATH go build -o cc-bootstrap ./cmd/bootstrap && \
  echo '[provision]   cc-server built; bind=' \$(grep -o '\"address\": \"[^\"]*\"' serverconfig.json) && ls -la cc-server"

# 3. Ship engine + uv sync (CUDA torch).
echo "[provision] (3/5) rsync engine + uv sync (CUDA torch)..."
rsync_box --delete \
  --exclude '.venv/' --exclude '__pycache__/' --exclude '.pytest_cache/' --exclude '*.pyc' \
  --exclude 'weights/' \
  "$ENGINE_SRC/" "$VAST_USER@$VAST_HOST:$REMOTE_DIR/engine/"
# NOT `uv sync`: the lock resolves PyPI torch (cu13x wheels — CPU-only on the 12.8-driver
# hosts) and the multi-GB download has stalled outright on some vast hosts. Reuse the
# template's working CUDA torch (/venv/main) via --system-site-packages and pip only the
# small pure-python deps.
ssh_box "cd '$REMOTE_DIR/engine' && rm -rf .venv && \
  /venv/main/bin/python -m venv --system-site-packages .venv && \
  .venv/bin/pip install -q --no-deps -e . && \
  .venv/bin/pip install -q httpx 'chess>=1.10' 'wandb>=0.17' 'numpy>=1.26' && \
  .venv/bin/python -c 'import torch; print(\"[provision]   torch\", torch.__version__, \"cuda=\", torch.cuda.is_available())'"

# 4. Seed live state (continue the run) -- the trainer warm-resumes from these.
if [ "$SEED_STATE" = "true" ]; then
  echo "[provision] (4/5) seeding state: db, networks/, trainer/run1/$([ "$SHIP_GAMES" = true ] && echo ', games/')..."
  ssh_box "mkdir -p '$REMOTE_DIR/lczero-server/trainer'"
  dbfiles=(); for f in chessckers.db chessckers.db-wal chessckers.db-shm; do [ -f "$f" ] && dbfiles+=("$f"); done
  [ ${#dbfiles[@]} -gt 0 ] && rsync_box "${dbfiles[@]}" "$VAST_USER@$VAST_HOST:$REMOTE_DIR/lczero-server/"
  rsync_box --delete networks/      "$VAST_USER@$VAST_HOST:$REMOTE_DIR/lczero-server/networks/"
  rsync_box --delete trainer/run1/  "$VAST_USER@$VAST_HOST:$REMOTE_DIR/lczero-server/trainer/run1/"
  [ "$SHIP_GAMES" = "true" ] && rsync_box --delete games/ "$VAST_USER@$VAST_HOST:$REMOTE_DIR/lczero-server/games/"
else
  echo "[provision] (4/5) SEED_STATE=false -> the run starts fresh on the box (cc-bootstrap makes run #1)."
fi

echo "[provision] (5/5) DONE."
echo "[provision] start it:  VAST_HOST=$VAST_HOST VAST_PORT=$VAST_PORT scripts/run_server_vast.sh"
