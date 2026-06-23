#!/usr/bin/env bash
# DESTRUCTIVE: wipe all chessckers fleet state so the next launch starts clean —
# fresh SQLite DB, no stored networks/games/pgns, empty trainer run dir, and the
# local client net cache. Run this whenever a bad net got bootstrapped (e.g. an
# old v1 net the engine can't load) so the trainer's next net becomes best #1.
#
# Stops any running fleet processes first, then wipes. Re-launch afterwards with
# launch_server.sh -> launch_trainer.sh -> (on each client) launch_client.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ "$(uname -s)" = Darwin ] && [ -z "${ON_BOX:-}" ]; then
  echo "[guard] This script runs on the Vast.ai GPU box, not locally." >&2
  echo "[guard] Use: python engine/scripts/cc.py <command>" >&2
  exit 1
fi

echo "[reset] stopping fleet processes..."
pkill -f cc-server 2>/dev/null || true
pkill -f trainer_bridge 2>/dev/null || true
pkill -f train_continuous 2>/dev/null || true
pkill -f 'akshay-chessckers-0 selfplay' 2>/dev/null || true
sleep 1

echo "[reset] wiping server state (db, networks, games, pgns, trainer runs)..."
rm -rf chessckers.db chessckers.db-wal chessckers.db-shm networks games pgns trainer/run*

# Nets are keyed by sha so a stale cache is harmless, but clear the LOCAL client
# cache for a truly clean start. Other client machines clear their own.
rm -rf "$HOME/Library/Caches/chessckers/client-cache/"* 2>/dev/null || true

echo "[reset] done. Re-launch:"
echo "  scripts/launch_server.sh         # tab 1"
echo "  scripts/launch_trainer.sh        # tab 2 (trains v2; uploads first net)"
echo "  scripts/launch_client.sh         # tab 3 (in lczero-client)"
