#!/usr/bin/env python3
"""One-shot status snapshot of the chessckers training fleet — the signals you
actually need to answer "is it training yet / why no new net?":

  - processes:   server / trainer_bridge / train_continuous up or down
  - games:       chunk count + total positions produced (server games dir)
  - publishing:  weights.bin mtime (= last net published)
  - networks:    how many nets the server has (DB), + last upload time

Run from anywhere:  scripts/fleet_status.py   (add --loop N to refresh every N s)
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
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


def _elo(score: float) -> float:
    """The in-fleet gate's calcElo: -400*log10(1/score - 1), capped ±800 (mirrors main.go)."""
    if score <= 0.0:
        return -800.0
    if score >= 1.0:
        return 800.0
    return max(-800.0, min(800.0, -400.0 * math.log10(1.0 / score - 1.0)))


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

    # Run identity: training_run id + description + games dir + gate threshold
    try:
        _rcon = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        _run_row = _rcon.execute(
            "SELECT id, description FROM training_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        _rcon.close()
        if _run_row:
            _rid, _rdesc = _run_row
            _games_base = games_dir.name
            _thr_seg = ""
            try:
                _cfg = json.loads((REPO / "serverconfig.json").read_text())
                _thr = _cfg.get("matches", {}).get("threshold")
                if _thr is not None:
                    _thr_val = int(_thr) if _thr == int(_thr) else _thr
                    _thr_seg = f" | gate thr {_thr_val}"
            except Exception:  # noqa: BLE001
                pass
            L.append(f'run:        #{_rid} "{_rdesc}" | games dir {_games_base}{_thr_seg}')
    except Exception:  # noqa: BLE001
        pass

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

    # trainer heartbeat (Phase 0 stats file) — the rates you tune cadence from
    stats_f = run_dir / "train_stats.json"
    try:
        st = json.loads(stats_f.read_text())
        L.append(
            f"trainer:    step {st['steps']} | {st['steps_per_s']:.1f} steps/s "
            f"{st['games_per_s']:.3f} games/s (stats {_age(st['updated'])})"
        )
    except (OSError, ValueError, KeyError):
        L.append(
            "trainer:    (no train_stats.json yet — training not started / pre-Phase0 trainer)"
        )

    # publishing
    wbin = run_dir / "weights.bin"
    if wbin.exists():
        age = _age(wbin.stat().st_mtime)
        L.append(f"last net:   weights.bin published {age}")
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
        # In-fleet promotion gate is LIVE (re-enabled 2026-06-13): each uploaded
        # candidate plays a fixed-N match vs the current best and is promoted only if
        # calcElo > -20. Completed gate matches are rows in `matches` with done=1,
        # test_only=0, deleted_at IS NULL (same filter `cc strength` reads). Surface
        # the headline (total / promoted / last verdict) here; full table = cc strength.
        gate = con.execute(
            "select count(*), coalesce(sum(passed), 0) from matches "
            "where done = 1 and test_only = 0 and deleted_at is null"
        ).fetchone()
        last = con.execute(
            "select c.network_number, b.network_number, m.wins, m.losses, m.draws, m.passed "
            "from matches m join networks c on c.id = m.candidate_id "
            "join networks b on b.id = m.current_best_id "
            "where m.done = 1 and m.test_only = 0 and m.deleted_at is null "
            "order by m.id desc limit 1"
        ).fetchone()
        # An open match (done=0) is the gate match currently in progress for the
        # latest candidate; only worth flagging because a *stuck* open match starves
        # self-play (nextGame).
        openm = con.execute(
            "select c.network_number, b.network_number, m.wins, m.losses, m.draws, m.game_cap "
            "from matches m join networks c on c.id = m.candidate_id "
            "join networks b on b.id = m.current_best_id where m.done = 0 "
            "order by m.id desc limit 1"
        ).fetchone()
        con.close()
        if best:
            num, sha = best
            L.append(
                f"best net:   #{num} (sha {sha}…) \033[2m<- clients run this; promoted via in-fleet gate\033[0m"
            )
        gmatches, gpromoted = gate or (0, 0)
        if gmatches:
            if last:
                cnum, bnum, w, l, d, passed = last
                n = w + l + d
                e = _elo((w + 0.5 * d) / n) if n else 0.0
                verdict = "\033[32mPROMOTE\033[0m" if passed else "\033[31mREJECT\033[0m"
                L.append(
                    f"matches:    in-fleet gate: {gmatches} run, {gpromoted} promoted | "
                    f"last #{cnum} vs #{bnum} {w}-{l}-{d} ({e:+.0f} Elo) {verdict} "
                    f"\033[2m(full table: cc strength)\033[0m"
                )
            else:
                L.append(
                    f"matches:    in-fleet gate: {gmatches} run, {gpromoted} promoted "
                    f"\033[2m(full table: cc strength)\033[0m"
                )
        else:
            L.append("matches:    none yet (gate live; awaiting first candidate)")
        if openm:
            cnum, bnum, w, l, d, cap = openm
            played = w + l + d
            L.append(
                f"\033[33m   gate match in progress: #{cnum} vs #{bnum} ({played}/{cap}) — "
                f"if stalled it starves self-play (close: UPDATE matches SET done=1 WHERE done=0)\033[0m"
            )
        L.append(f"server db:  {nets} networks, {dbgames} games recorded")
    except Exception as e:  # noqa: BLE001
        L.append(f"server db:  (unreadable: {e})")

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
