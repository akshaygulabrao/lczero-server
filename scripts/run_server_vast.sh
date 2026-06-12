#!/usr/bin/env bash
# Start (or restart) the chessckers SERVER + TRAINER on the provisioned vast box,
# in a detached tmux session so they survive ssh drops. Run FROM the Mac after
# provision_server_vast.sh. Idempotent: kills any prior 'cc' session first.
#
#   window 'server'  -> scripts/launch_server.sh   (go build, cc-bootstrap no-op
#                       since the run is seeded, then cc-server on 0.0.0.0:9830)
#   window 'trainer' -> scripts/launch_trainer.sh  (trainer_bridge -> train_continuous;
#                       pick_device(auto)=cuda, warm-resumes from trainer/run1/weights.pt)
#
# Usage: VAST_HOST=sshN.vast.ai VAST_PORT=23456 scripts/run_server_vast.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VAST_HOST="${VAST_HOST:?set VAST_HOST}"
VAST_PORT="${VAST_PORT:?set VAST_PORT}"
VAST_USER="${VAST_USER:-root}"
REMOTE_DIR="${REMOTE_DIR:-/workspace/chessckers}"
RUN_NAME="${RUN_NAME:-V4_e8d8}"
SERVER_PORT="${SERVER_PORT:-10100}"   # must match provision_server_vast.sh (cc-server's bind port)
SSHO="-p $VAST_PORT -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20"

echo "[run] starting tmux session 'cc' on $VAST_USER@$VAST_HOST:$VAST_PORT (server :$SERVER_PORT)..."
ssh $SSHO "$VAST_USER@$VAST_HOST" "REMOTE_DIR='$REMOTE_DIR' RUN_NAME='$RUN_NAME' SERVER_PORT='$SERVER_PORT' bash -s" <<'REMOTE'
set -euo pipefail
SRV="$REMOTE_DIR/lczero-server"
ENG="$REMOTE_DIR/engine"
[ -x "$SRV/cc-server" ] || { echo "[run] $SRV/cc-server not found -- run provision_server_vast.sh first" >&2; exit 1; }
tmux kill-session -t cc 2>/dev/null || true
# server window: launch_server.sh runs `go build` -> needs Go on PATH.
tmux new-session -d -s cc -n server -c "$SRV"
tmux send-keys -t cc:server "cd '$SRV' && PATH=/usr/local/go/bin:\$PATH RUN_NAME='$RUN_NAME' scripts/launch_server.sh 2>&1 | tee -a server.log" C-m
# trainer window: ENGINE_DIR points at the box's engine; bridge uploads to localhost.
# sleep gives the server time to bind :9830 + bootstrap before the first upload poll.
tmux new-window -t cc -n trainer -c "$SRV"
tmux send-keys -t cc:trainer "cd '$SRV' && sleep 6 && ENGINE_DIR='$ENG' SERVER=http://localhost:$SERVER_PORT scripts/launch_trainer.sh 2>&1 | tee -a trainer.log" C-m
echo "[run] tmux 'cc' started (windows: server, trainer)."
REMOTE

cat <<EOF
[run] -------------------------------------------------------------------------
[run] server is listening on 0.0.0.0:$SERVER_PORT INSIDE the container (an already-open vast port).
[run] Clients use the instance's PUBLIC ip + the EXTERNAL port vast mapped to $SERVER_PORT:
[run]   find it in the vast UI "IP Port Info"  ("<public-ip>:<ext> -> $SERVER_PORT/tcp")
[run]   or:  vastai show instance <id>  (ports).
[run]   then on each client (mac / leena / other vast box):
[run]     SERVER=http://<public-ip>:<ext-port> scripts/launch_client.sh
[run]
[run] watch:  ssh -p $VAST_PORT $VAST_USER@$VAST_HOST -t tmux attach -t cc   (Ctrl-b d to detach)
[run] stop :  attach, Ctrl-C the trainer window FIRST (clean replay snapshot), then:
[run]         ssh -p $VAST_PORT $VAST_USER@$VAST_HOST tmux kill-session -t cc
[run] -------------------------------------------------------------------------
EOF
