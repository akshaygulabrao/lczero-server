#!/usr/bin/env bash
# Relaunch the chessckers fleet (server + trainer WARM-resume + client) in tmux.
#
# Idempotent: a no-op if both sessions are already alive. Used by the @reboot cron
# AND `cc restart`, so a vast.ai reboot — which kills tmux but leaves disk state
# intact — self-heals instead of silently killing the run. The trainer warm-resumes
# from trainer/run1/weights.pt (the current run's net); NO rebuild, NO wipe.
#
#   restart_fleet.sh           # manual: relaunch now if down
#   restart_fleet.sh --boot    # @reboot: wait BOOT_WAIT first (box settles)
#
# Overridable via env: RUN_NAME, ARCH_VERSION, C_FILTERS/N_BLOCKS/SE_RATIO,
# POLICY_TARGET, VALUE_Q_RATIO, PARALLELISM, BOOT_WAIT.
set -uo pipefail
SRV=/workspace/chessckers/lczero-server
ENG=/workspace/chessckers/engine
CL=/workspace/chessckers/lczero-client
RUN_NAME="${RUN_NAME:-resume}"
ARCH_VERSION="${ARCH_VERSION:-v5}"
# Net SIZE must match the published weights.pt the trainer warm-resumes, or it
# SIGTRAPs loading a mismatched-shape net. Default to the CURRENT run's arch
# (runs 11+ = c64/b6) so a bare reboot can't silently revert to launch_trainer.sh's
# c48/b5 default. The @reboot cron passes these explicitly too.
C_FILTERS="${C_FILTERS:-64}"
N_BLOCKS="${N_BLOCKS:-6}"
SE_RATIO="${SE_RATIO:-8}"
# Training-target knobs — same rationale as the arch dims: default to the CURRENT
# run's values (run 15 = Gumbel improved-policy target, pure-z value) so a bare
# reboot can't silently flip the A/B arm back to visits / q-ratio 0.5.
POLICY_TARGET="${POLICY_TARGET:-improved}"
VALUE_Q_RATIO="${VALUE_Q_RATIO:-0}"
PARALLELISM="${PARALLELISM:-32}"
export PATH=/usr/local/go/bin:/usr/bin:/usr/local/bin:/usr/sbin:$PATH

log() { echo "[$(date '+%F %T')] [restart] $*"; }

# @reboot: vast services "stay down during provisioning"; wait for the box to
# settle (CUDA up, /workspace synced) before launching.
if [ "${1:-}" = "--boot" ]; then
  log "boot mode — waiting ${BOOT_WAIT:-60}s for the box to settle"
  sleep "${BOOT_WAIT:-60}"
fi

# Already running? Don't double-launch.
if tmux has-session -t cc 2>/dev/null && tmux has-session -t cc-client 2>/dev/null; then
  log "fleet already running (cc + cc-client) — nothing to do"
  exit 0
fi

[ -x "$SRV/cc-server" ] || { log "ABORT: $SRV/cc-server missing — provision first (not a plain reboot)"; exit 1; }

# A reboot can truncate the in-flight chunk to 0 bytes, which breaks ingestion.
n=$(find "$SRV/games/run1" -name 'training.*.gz' -size 0 -print -delete 2>/dev/null | wc -l)
[ "$n" -gt 0 ] && log "removed $n truncated 0-byte chunk(s)"

tmux kill-session -t cc 2>/dev/null || true
tmux kill-session -t cc-client 2>/dev/null || true
sleep 1

# server — resumes the existing DB/nets state on disk (RUN_NAME is just a label)
tmux new-session -d -s cc -n server -c "$SRV"
tmux send-keys -t cc:server "cd $SRV && PATH=/usr/local/go/bin:\$PATH RUN_NAME=$RUN_NAME scripts/launch_server.sh 2>&1 | tee -a server.log" C-m
# trainer — auto-warm-resume from trainer/run1/weights.pt (the current run's net)
tmux new-window -t cc -n trainer -c "$SRV"
tmux send-keys -t cc:trainer "cd $SRV && sleep 10 && ENGINE_DIR=$ENG SERVER=http://localhost:10100 ARCH_VERSION=$ARCH_VERSION C_FILTERS=$C_FILTERS N_BLOCKS=$N_BLOCKS SE_RATIO=$SE_RATIO POLICY_TARGET=$POLICY_TARGET VALUE_Q_RATIO=$VALUE_Q_RATIO scripts/launch_trainer.sh 2>&1 | tee -a trainer.log" C-m
# self-play client
tmux new-session -d -s cc-client -n selfplay -c "$CL"
tmux send-keys -t cc-client "export PATH=$CL/.enginebin:\$PATH; cd $CL; ./lc0-client -hostname http://localhost:10100 -user vast -password chessckers -run 1 -parallelism $PARALLELISM 2>&1 | tee -a client.log" C-m

log "relaunched cc (server+trainer) + cc-client  [run=$RUN_NAME arch=$ARCH_VERSION c=$C_FILTERS b=$N_BLOCKS target=$POLICY_TARGET qratio=$VALUE_Q_RATIO p=$PARALLELISM]"
