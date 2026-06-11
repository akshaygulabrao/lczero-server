#!/usr/bin/env python3
"""Trainer bridge: close the chessckers distributed-training loop.

The Go server (this repo) DISPATCHES self-play work and STORES the gzipped-JSON
``ccz1`` game chunks that clients upload, but it does no training (just like
lczero-server: training is an external process). The chessckers engine already
has a continuous AlphaZero trainer, ``chessckers_engine.train_continuous``, which
watches a ``buffer/`` directory of ccz1 chunks, trains nonstop, and publishes a
native ``weights.bin`` (C++-loadable by ``akshay-chessckers-0``) on a timer.

This bridge is the glue between the two — pure file-plumbing + HTTP, no torch:

  1. spawns ``train_continuous`` (engine venv) pointed at ``<run-dir>``,
  2. FEEDS: copies each new server chunk ``<games-dir>/training.N.gz`` into
     ``<run-dir>/buffer/training.N.pkl`` (the trainer drains + deletes them; the
     server keeps the originals). A persisted high-water mark avoids re-feeding,
  3. PUBLISHES: when ``<run-dir>/weights.bin`` changes, gzips it and POSTs it to
     ``/upload_network``. The server sha256s the *decompressed* bytes, stores it,
     and either bootstraps it as best (first net) or opens a promotion match.

train_continuous publishes an initial (random-init) net immediately on startup,
so the very first upload gives clients something to self-play with; from then on
the loop is self-sustaining: dispatch -> play -> upload_game -> feed -> train ->
weights.bin -> upload_network -> promote -> dispatch the better net.

Run it on the SAME machine as the server (it reads the server's games/ dir and
uploads to localhost). See scripts/launch_trainer.sh.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

_CHUNK_RE = re.compile(r"training\.(\d+)\.gz$")


def _post_multipart(url: str, fields: dict[str, str], file_field: str,
                    file_name: str, file_bytes: bytes, timeout: float = 180.0):
    """Minimal multipart/form-data POST (stdlib only, no requests dependency)."""
    boundary = "----chessckersbridgeboundary7f3a"
    pre = b""
    for k, v in fields.items():
        pre += (f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n').encode()
    pre += (f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{file_name}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body = pre + file_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def feed_new_chunks(games_dir: Path, buffer_dir: Path, hwm: int) -> int:
    """Copy server chunks training.N.gz (N > hwm) into the trainer buffer as
    training.N.pkl. Returns the new high-water mark. The trainer decodes ccz1 by
    content (gzip magic), so the .pkl extension is just the buffer's glob."""
    if not games_dir.exists():
        return hwm
    pending = []
    for f in games_dir.glob("training.*.gz"):
        m = _CHUNK_RE.search(f.name)
        if m and int(m.group(1)) > hwm:
            pending.append((int(m.group(1)), f))
    pending.sort()
    for n, f in pending:
        dst = buffer_dir / f"training.{n}.pkl"
        try:
            # Copy to a temp name then rename so the trainer never decodes a
            # half-copied file (it would just skip+retry, but this avoids churn).
            tmp = dst.with_suffix(".pkl.partial")
            shutil.copyfile(f, tmp)
            os.replace(tmp, dst)
            hwm = n
        except OSError as e:
            print(f"[bridge] feed failed for {f.name}: {e}", flush=True)
            break
    return hwm


def upload_net(server: str, weights_bin: Path, training_id: int) -> bool:
    """Gzip weights.bin and POST it to /upload_network. Returns True on a fresh
    upload, False on a dedup ('already exists') or error (both non-fatal)."""
    try:
        raw = weights_bin.read_bytes()
    except OSError as e:
        print(f"[bridge] cannot read {weights_bin}: {e}", flush=True)
        return False
    gz = gzip.compress(raw, compresslevel=6, mtime=0)
    try:
        status, text = _post_multipart(
            server.rstrip("/") + "/upload_network",
            {"training_id": str(training_id)},
            "file", "weights.bin.gz", gz)
    except urllib.error.URLError as e:
        print(f"[bridge] upload_network failed: {e}", flush=True)
        return False
    text = text.strip()
    if status == 200:
        print(f"[bridge] uploaded net ({len(raw)} bytes raw): {text}", flush=True)
        return True
    # 400 "Network already exists" is expected when weights.bin hasn't changed
    # in content (e.g. a bridge restart); not an error.
    print(f"[bridge] upload_network HTTP {status}: {text}", flush=True)
    return False


def main() -> int:
    p = argparse.ArgumentParser(description="Chessckers trainer bridge (ccz1 games -> net).")
    p.add_argument("--server", default="http://localhost:9830",
                   help="server base URL (the bridge runs on the same host)")
    p.add_argument("--games-dir", required=True, type=Path,
                   help="server's stored chunks, e.g. <server-repo>/games/run1")
    p.add_argument("--run-dir", required=True, type=Path,
                   help="trainer run dir (buffer/, weights.bin live here)")
    p.add_argument("--engine-dir", required=True, type=Path,
                   help="chessckers engine repo (has .venv + chessckers_engine)")
    p.add_argument("--training-id", type=int, default=1)
    p.add_argument("--poll-seconds", type=float, default=5.0)
    p.add_argument("--base", default="", help="warm-start checkpoint for the trainer (.pt)")
    p.add_argument("--publish-seconds", type=float, default=45.0,
                   help="trainer weights.bin publish cadence FLOOR (0 = off; prefer step/game triggers)")
    # Phase 1: progress-based publish triggers (passed straight to train_continuous).
    p.add_argument("--publish-steps", type=int, default=0,
                   help="publish after this many SGD steps since the last publish (0 = off)")
    p.add_argument("--publish-games", type=int, default=0,
                   help="publish after this many games ingested since the last publish (0 = off)")
    # Phase 2: lc0-style generational training.
    p.add_argument("--window-games", type=int, default=0,
                   help="cap the replay window to the last N games (0 = use --buffer-cap positions). "
                        "With --window-games-min set, this is the ramp CEILING.")
    p.add_argument("--window-games-min", type=int, default=0,
                   help="lc0/KataGo growing window: ramp the window from this many games up to "
                        "--window-games as self-play accumulates (0 = fixed window, no ramp)")
    p.add_argument("--window-ramp-alpha", type=float, default=0.75,
                   help="growth exponent for the window ramp (KataGo default 0.75; lower = slower)")
    p.add_argument("--lr", type=float, default=2e-2,
                   help="base LR (SGD-scale; train_continuous uses SGD+Nesterov)")
    p.add_argument("--lr-warmup-steps", type=int, default=0)
    p.add_argument("--lr-decay-steps", type=int, default=0)
    p.add_argument("--lr-gamma", type=float, default=0.5)
    p.add_argument("--ema-decay", type=float, default=0.0,
                   help="publish an EMA of the weights (lc0-SWA analog); 0 = raw weights")
    p.add_argument("--buffer-cap", type=int, default=50000)
    p.add_argument("--batch-size", type=int, default=256)
    p.add_argument("--min-buffer", type=int, default=2000)
    p.add_argument("--buffer-snapshot-seconds", type=float, default=0.0,
                   help="also snapshot the replay buffer every N s for crash safety (0 = "
                        "shutdown-only; the clean-shutdown snapshot is always written)")
    p.add_argument("--replay-factor", type=float, default=8.0,
                   help="cap total samples at this x positions-ingested. Raise when the "
                        "trainer is generation-bound (idle waiting on self-play) to take "
                        "more SGD steps on existing games; 0 = unthrottled")
    p.add_argument("--value-discount", type=float, default=1.0,
                   help="per-ply WDL value discount gamma (<1 incentivizes faster wins); 1.0 = off")
    p.add_argument("--value-q-ratio", type=float, default=0.0,
                   help="blend the search root value q into the value target: (1-r)*z + r*q "
                        "(needs lc0-fork chunks carrying search_wdl); 0.0 = off (pure outcome z)")
    # NN architecture handed to the trainer. MUST be v2: akshay-chessckers-0
    # encodes 16 position planes (the v2 representation); a v1 net has a
    # 15-channel input conv, so the engine reads 16 planes into a 15-channel
    # weight and SIGTRAPs on the first eval. v1 nets are NOT engine-loadable.
    p.add_argument("--arch-version", choices=["v1", "v2", "v4"], default="v2")
    p.add_argument("--c-filters", type=int, default=96)
    p.add_argument("--n-blocks", type=int, default=4)
    p.add_argument("--d-hidden", type=int, default=256)
    p.add_argument("--tf-blocks", type=int, default=0,
                   help="v2/v4 transformer blocks interleaved into the trunk (0 = pure ResNet)")
    p.add_argument("--se-ratio", type=int, default=8,
                   help="v4: Squeeze-Excitation reduction ratio")
    p.add_argument("--no-trainer", action="store_true",
                   help="do NOT spawn train_continuous (assume it runs elsewhere); just feed+upload")
    args = p.parse_args()

    run_dir: Path = args.run_dir.resolve()
    buffer_dir = run_dir / "buffer"
    weights_bin = run_dir / "weights.bin"
    buffer_dir.mkdir(parents=True, exist_ok=True)
    state_file = run_dir / "bridge_state.json"

    # Remove any weights.bin left over from a previous run BEFORE the trainer
    # starts. Otherwise our first poll could upload that stale net — possibly an
    # incompatible v1 from an earlier run — which the server would bootstrap as
    # best and the engine would SIGTRAP on, all before train_continuous overwrites
    # it with this run's (v2) net. (Skip when --no-trainer: then weights.bin is
    # produced by a trainer running elsewhere and we must not delete it.)
    if not args.no_trainer:
        try:
            weights_bin.unlink()
        except FileNotFoundError:
            pass

    # Resume the feed high-water mark so a restart doesn't re-feed already-ingested
    # (and deleted) chunks.
    hwm = 0
    try:
        hwm = int(json.loads(state_file.read_text()).get("hwm", 0))
    except (OSError, ValueError):
        pass

    venv_py = args.engine_dir / ".venv" / "bin" / "python"
    python = str(venv_py) if venv_py.exists() else sys.executable

    trainer = None
    if not args.no_trainer:
        cmd = [python, "-m", "chessckers_engine.train_continuous",
               "--run-dir", str(run_dir),
               "--arch-version", args.arch_version,
               "--c-filters", str(args.c_filters),
               "--n-blocks", str(args.n_blocks),
               "--d-hidden", str(args.d_hidden),
               "--tf-blocks", str(args.tf_blocks),
               "--se-ratio", str(args.se_ratio),
               "--buffer-cap", str(args.buffer_cap),
               "--batch-size", str(args.batch_size),
               "--min-buffer", str(args.min_buffer),
               "--buffer-snapshot-seconds", str(args.buffer_snapshot_seconds),
               "--replay-factor", str(args.replay_factor),
               "--value-discount", str(args.value_discount),
               "--value-q-ratio", str(args.value_q_ratio),
               "--publish-seconds", str(args.publish_seconds),
               "--publish-steps", str(args.publish_steps),
               "--publish-games", str(args.publish_games),
               "--window-games", str(args.window_games),
               "--window-games-min", str(args.window_games_min),
               "--window-ramp-alpha", str(args.window_ramp_alpha),
               "--lr", str(args.lr),
               "--lr-warmup-steps", str(args.lr_warmup_steps),
               "--lr-decay-steps", str(args.lr_decay_steps),
               "--lr-gamma", str(args.lr_gamma),
               "--ema-decay", str(args.ema_decay),
               "--ckpt-seconds", "300"]
        if args.base:
            cmd += ["--base", args.base]
        print(f"[bridge] starting trainer: {' '.join(cmd)}", flush=True)
        trainer = subprocess.Popen(cmd, cwd=str(args.engine_dir))

    stop = {"flag": False}

    def _on_term(*_a):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _on_term)
    signal.signal(signal.SIGTERM, _on_term)

    last_bin_mtime = 0.0
    print(f"[bridge] up: server={args.server} games={args.games_dir} run={run_dir} "
          f"hwm={hwm}", flush=True)
    try:
        while not stop["flag"]:
            new_hwm = feed_new_chunks(args.games_dir, buffer_dir, hwm)
            if new_hwm != hwm:
                hwm = new_hwm
                try:
                    state_file.write_text(json.dumps({"hwm": hwm}))
                except OSError:
                    pass

            if weights_bin.exists():
                mtime = weights_bin.stat().st_mtime
                if mtime > last_bin_mtime:
                    if upload_net(args.server, weights_bin, args.training_id):
                        last_bin_mtime = mtime
                    else:
                        # Avoid hammering on dedup/transient errors: treat as seen
                        # but retry on the NEXT publish (mtime will advance).
                        last_bin_mtime = mtime

            # If the trainer died, surface it and exit.
            if trainer is not None and trainer.poll() is not None:
                print(f"[bridge] trainer exited with {trainer.returncode}; stopping", flush=True)
                break

            time.sleep(args.poll_seconds)
    finally:
        if trainer is not None and trainer.poll() is None:
            # Graceful: train_continuous stops on a STOP file or SIGTERM.
            (run_dir / "STOP").touch()
            try:
                trainer.wait(timeout=30)
            except subprocess.TimeoutExpired:
                trainer.terminate()
        print("[bridge] stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
