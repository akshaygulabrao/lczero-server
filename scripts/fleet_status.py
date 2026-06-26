#!/usr/bin/env python3
"""One-shot status snapshot of the chessckers training fleet — the signals you
actually need to answer "is it training yet / why no new net?":

  - processes:   server / trainer_bridge / train_continuous up or down
  - games:       chunk count + total positions produced (server games dir)
  - cold start:  positions vs the LIVE --min-buffer (read from the trainer's
                 args) — the threshold below which the trainer takes NO SGD step
  - publishing:  weights.bin mtime (= last net published) vs --publish-seconds
  - networks:    how many nets the server has (DB), + last upload time

Run from anywhere:  scripts/fleet_status.py   (add --loop N to refresh every N s)
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sqlite3
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent  # lczero-server repo root

# Cap on how many PGN files the balance scan reads (most-recent by mtime). The
# box accumulates one .pgn per self-play game, so an unbounded scan would walk
# tens of thousands of tiny files every refresh. We log when we truncate rather
# than silently sampling a subset.
PGN_SCAN_CAP = 2000
_MOVE_NUM_RE = re.compile(r"^\d+\.+$")  # PGN move-number tokens like "12." / "12..."


def _percentile(sorted_vals: list[int], q: float) -> int:
    """Nearest-rank percentile (q in [0,1]) of an already-sorted list."""
    if not sorted_vals:
        return 0
    idx = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
    return sorted_vals[idx]


def _parse_pgn_result_and_len(text: str) -> tuple[str | None, int] | None:
    """From one PGN's text, return (result_token, ply_count) or None if no result.

    The client writes movetext as space-joined move tokens followed by the
    absolute result token (`1-0` White win / `0-1` Black win / `1/2-1/2` draw),
    then a ` {OL: N}` comment; an optional leading `[FEN "..."]` header block is
    present when the game started from an opening book. We ignore the wdl tensor
    entirely — the start FEN is Black-to-move, so a wdl-derived W/B split would
    invert. ply_count = number of move tokens (each token is one half-move)."""
    result = None
    ply = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("[") or line.startswith("{"):
            continue  # header tags / stray comment lines
        # Strip any trailing "{...}" comment (e.g. "{OL: 6}") from the line.
        brace = line.find("{")
        if brace != -1:
            line = line[:brace].strip()
        for tok in line.split():
            if tok in ("1-0", "0-1", "1/2-1/2"):
                result = tok
                continue
            if tok == "*":  # unfinished game, no result
                continue
            if _MOVE_NUM_RE.match(tok):  # "1." style move numbers, if any
                continue
            ply += 1
    if result is None:
        return None
    return result, ply


def pgn_balance(pgn_dir: Path, cap: int = PGN_SCAN_CAP) -> str | None:
    """One-line White/Black/draw win-rate + game-length summary, derived purely
    from the result token of the most-recent `cap` PGNs (by mtime). Returns None
    if the dir is absent/empty so the caller can skip the line."""
    if not pgn_dir.exists():
        return None
    files = list(pgn_dir.glob("*.pgn"))
    if not files:
        return None
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    truncated = len(files) > cap
    scan = files[:cap]

    white = black = draw = 0
    lengths: list[int] = []
    for f in scan:
        try:
            parsed = _parse_pgn_result_and_len(f.read_text())
        except OSError:  # mid-write / vanished; skip
            continue
        if parsed is None:
            continue
        result, ply = parsed
        if result == "1-0":
            white += 1
        elif result == "0-1":
            black += 1
        else:
            draw += 1
        lengths.append(ply)

    n = white + black + draw
    if n == 0:
        return None
    # Length trend: compare newer half vs older half
    half = max(1, len(lengths) // 2)
    newer_avg = (
        sum(lengths[:half]) / half
    )  # files sorted by mtime desc, so [:half] = newer
    older_avg = (
        sum(lengths[half:]) / (len(lengths) - half)
        if len(lengths) > half
        else newer_avg
    )
    diff = newer_avg - older_avg
    arrow = "↑" if diff > 3 else "↓" if diff < -3 else "→"
    lengths.sort()
    p50 = _percentile(lengths, 0.50)
    p95 = _percentile(lengths, 0.95)
    trunc = (
        f"  \033[33m[scanned newest {cap} of {len(files)} pgns]\033[0m"
        if truncated
        else ""
    )
    return (
        f"balance:    White {100 * white / n:.0f}% / Black {100 * black / n:.0f}% / "
        f"draw {100 * draw / n:.0f}%  | len p50={p50} p95={p95} "
        f"{arrow} {diff:+.1f} ({n} games){trunc}"
    )


def _proc_args(needle: str) -> list[str] | None:
    """argv of the first running process whose cmdline contains `needle`. Uses
    `ps` (portable: macOS `pgrep` has no `-a`/full-cmdline-listing flag)."""
    try:
        out = subprocess.run(
            ["ps", "-axo", "args="], capture_output=True, text=True
        ).stdout
    except FileNotFoundError:
        return None
    for line in out.splitlines():
        if needle in line and "fleet_status" not in line:  # skip ourselves
            return line.split()
    return None


def _flag(argv: list[str] | None, name: str, default):
    if not argv:
        return default
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return default


def _age(mtime: float) -> str:
    s = max(0, int(time.time() - mtime))
    return f"{s}s ago" if s < 90 else f"{s // 60}m{s % 60:02d}s ago"


def snapshot(
    games_dir: Path, run_dir: Path, db_path: Path, pgn_dir: Path | None = None
) -> str:
    L: list[str] = []
    server = _proc_args("cc-server")
    bridge = _proc_args("trainer_bridge.py")
    trainer = _proc_args("train_continuous")

    def updown(p):
        return "\033[32mUP\033[0m" if p else "\033[31mDOWN\033[0m"

    L.append(
        f"processes:  server {updown(server)}   bridge {updown(bridge)}   "
        f"trainer {updown(trainer)}"
    )

    # live config from the trainer's own args (falls back to known defaults)
    min_buffer = int(_flag(trainer, "--min-buffer", 2000))
    publish_s = float(_flag(trainer, "--publish-seconds", 45.0))
    batch = int(_flag(trainer, "--batch-size", 1024))
    replay_factor = float(_flag(trainer, "--replay-factor", 8.0))
    tf_blocks = _flag(trainer, "--tf-blocks", "?")
    se_ratio = _flag(trainer, "--se-ratio", "?")
    arch = _flag(trainer, "--arch-version", "?")

    # games + positions produced
    chunks = (
        sorted(games_dir.glob("training.*.gz"), key=lambda p: int(p.stem.split(".")[1]))
        if games_dir.exists()
        else []
    )
    positions = 0
    for f in chunks:
        try:
            positions += len(json.loads(gzip.open(f).read()).get("examples") or [])
        except Exception:  # noqa: BLE001 — a chunk mid-write; just skip it
            pass
    latest = f"  (latest {_age(chunks[-1].stat().st_mtime)})" if chunks else ""
    L.append(f"games:      {len(chunks)} chunks, {positions} positions{latest}")

    # White/Black/draw split + game length, from the PGN result tokens (NOT the
    # wdl tensor: start FEN is Black-to-move so wdl would invert W/B).
    if pgn_dir is not None:
        bal = pgn_balance(pgn_dir)
        if bal:
            L.append(bal)

    # cold-start gate
    if positions >= min_buffer:
        L.append(
            f"cold start: \033[32mCLEARED\033[0m  ({positions} >= min-buffer {min_buffer}) "
            f"— SGD running"
        )
    else:
        pct = 100 * positions / min_buffer if min_buffer else 0
        L.append(
            f"cold start: \033[33mWAITING\033[0m  {positions}/{min_buffer} "
            f"({pct:.0f}%, {min_buffer - positions} short) — no SGD until cleared"
        )

    # trainer heartbeat (Phase 0 stats file) — the rates you tune cadence from
    stats_f = run_dir / "train_stats.json"
    try:
        st = json.loads(stats_f.read_text())
        L.append(
            f"trainer:    step {st['steps']} | {st['steps_per_s']:.1f} steps/s "
            f"{st['games_per_s']:.3f} games/s | lr={st['lr']:.2e} "
            f"(stats {_age(st['updated'])})"
        )
        # reuse balance: are games generated faster than the trainer trains on them?
        # trained positions/s = steps/s * batch ; generated positions/s = games/s * pos-per-game.
        # actual reuse = trained/generated, compared to the configured --replay-factor target.
        # (replay-factor THROTTLES steps at replay_factor x positions_ingested, so target is
        # the ceiling the trainer aims for; actual << target => trainer can't keep up.)
        gps = st.get("games_per_s") or 0.0
        sps = st.get("steps_per_s") or 0.0
        pos_ing = st.get("positions_ingested") or 0
        games_seen = st.get("games_seen") or 0
        pos_per_game = (pos_ing / games_seen) if games_seen else 0.0
        trained_pps = sps * batch
        gen_pps = gps * pos_per_game
        reuse = (trained_pps / gen_pps) if gen_pps else 0.0
        if replay_factor and reuse:
            if reuse >= 0.85 * replay_factor:
                tag = "\033[32mKEEPING UP\033[0m (trainer at/above target; may be over-generating)"
            elif reuse >= 0.5 * replay_factor:
                tag = "\033[33mLAGGING\033[0m (below replay target — some games under-trained)"
            else:
                tag = "\033[31mFALLING BEHIND\033[0m (generating games far faster than trained)"
            L.append(
                f"throughput: train {trained_pps:.0f} pos/s  vs  gen {gen_pps:.0f} pos/s "
                f"({pos_per_game:.0f} pos/game)"
            )
            L.append(
                f"reuse:      {reuse:.1f}x actual  vs  {replay_factor:.0f}x target "
                f"(--replay-factor) — {tag}"
            )
    except (OSError, ValueError, KeyError):
        L.append(
            "trainer:    (no train_stats.json yet — SGD not started / pre-Phase0 trainer)"
        )

    # publishing
    wbin = run_dir / "weights.bin"
    if wbin.exists():
        age = _age(wbin.stat().st_mtime)
        # progress-gated publishing means time-staleness isn't meaningful unless a
        # time floor is set; only warn when --publish-seconds is the active trigger.
        stale = publish_s > 0 and (time.time() - wbin.stat().st_mtime) > 3 * publish_s
        warn = "  \033[33m(stale — trainer not publishing)\033[0m" if stale else ""
        L.append(f"last net:   weights.bin published {age}{warn}")
    else:
        L.append("last net:   weights.bin MISSING (no publish yet)")

    # networks the server has + WHICH net is best (what every client is running)
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        nets = con.execute("select count(*) from networks").fetchone()[0]
        dbgames = con.execute("select count(*) from training_games").fetchone()[0]
        best = con.execute(
            "select n.network_number, substr(n.sha,1,12) "
            "from training_runs t join networks n on n.id = t.best_network_id"
        ).fetchone()
        # Arenas were removed (every uploaded net auto-promotes; see uploadNetwork).
        # No new matches are created, so the only matches with done=0 are LEGACY
        # leftovers — and any open match still starves self-play (nextGame). Surface
        # them as a warning to be closed, not as healthy "arena progress".
        m = con.execute(
            "select c.network_number, b.network_number, m.wins, m.losses, m.draws, m.game_cap "
            "from matches m join networks c on c.id = m.candidate_id "
            "join networks b on b.id = m.current_best_id where m.done = 0 "
            "order by m.id desc limit 1"
        ).fetchone()
        con.close()
        if best:
            num, sha = best
            L.append(
                f"best net:   #{num} (sha {sha}…) \033[2m<- clients run this; auto-promoted (arenas removed)\033[0m"
            )
        if m:
            cnum, bnum, w, l, d, cap = m
            played = w + l + d
            L.append(
                f"\033[31mmatches:    LEGACY open match #{cnum} vs #{bnum} ({played}/{cap}) — "
                f"starving self-play; close it: UPDATE matches SET done=1 WHERE done=0\033[0m"
            )
        else:
            L.append("matches:    none (arenas removed — every net auto-promotes)")
        L.append(f"server db:  {nets} networks, {dbgames} games recorded")
    except Exception as e:  # noqa: BLE001
        L.append(f"server db:  (unreadable: {e})")

    se_desc = f"SE(r{se_ratio})" if arch == "v4" else f"tf-blocks {tf_blocks}"
    L.append(f"arch:       {arch} ({se_desc})")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="chessckers fleet status snapshot")
    ap.add_argument("--training-id", type=int, default=1)
    ap.add_argument("--games-dir", default="")
    ap.add_argument("--run-dir", default="")
    ap.add_argument("--pgn-dir", default="")
    ap.add_argument("--db", default=str(REPO / "chessckers.db"))
    ap.add_argument(
        "--loop", type=float, default=0.0, help="refresh every N seconds (0 = once)"
    )
    args = ap.parse_args()

    games_dir = Path(args.games_dir or REPO / f"games/run{args.training_id}")
    run_dir = Path(args.run_dir or REPO / f"trainer/run{args.training_id}")
    pgn_dir = Path(args.pgn_dir or REPO / f"pgns/run{args.training_id}")
    db_path = Path(args.db)

    while True:
        snap = snapshot(games_dir, run_dir, db_path, pgn_dir)
        if args.loop:
            print("\033[2J\033[H", end="")  # clear screen + home cursor
            print(
                f"=== chessckers fleet status (run {args.training_id}) "
                f"{time.strftime('%H:%M:%S')} ==="
            )
        print(snap)
        if not args.loop:
            return 0
        time.sleep(args.loop)


if __name__ == "__main__":
    raise SystemExit(main())
