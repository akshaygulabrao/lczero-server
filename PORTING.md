# Porting lczero-server → chessckers (akshay-chessckers-0 fleet)

This is the LeelaChessZero distributed-training **server** converted to dispatch
and collect games for **Chessckers** (the 10×10 chess-vs-checkers hybrid played by
the `akshay-chessckers-0` engine). Same approach as the engine port: keep
everything lc0 already got right (the HTTP dispatch protocol, token sharding,
match scheduling, Elo/promotion, the web dashboard) and swap only the
chess-specific payloads and the Postgres assumptions.

## Decisions (locked)

1. **SQLite, not Postgres.** A personal tailnet fleet doesn't need a Postgres
   daemon. The GORM models port unchanged; only the driver + a handful of
   Postgres-only raw queries changed. `serverconfig.json:database.dbname` is now
   the path to the SQLite file (`chessckers.db`).
2. **Keep the transport, swap the payloads.** The server still treats game chunks
   and networks as opaque hashed blobs — so the chessckers `ccz1` training chunks
   (gzipped JSON) and `.bin` networks flow through `upload_game` / `upload_network`
   / `get_network` unchanged. Dispatch, matches, Elo, and the dashboard generalize.
3. **Tailscale for orchestration.** The server binds `:9830`; clients reach it at
   the server's tailnet MagicDNS name (e.g. `http://macbookprom1pro:9830`). Tailnet
   traffic rides a WireGuard `utun`, not the LAN, so macOS Local-Network privacy
   (TCC) never gates a remote (e.g. leena) self-play client — the blocker the LAN
   fleet hit does not occur.

## What changed (and why)

- **Go modules + import paths.** The repo was pre-modules (bare `import "db"` /
  `"config"` resolved via a repo-root `GOPATH`). Added a `go.mod`; rewrote those to
  `github.com/LeelaChessZero/lczero-server/src/{db,config}`. The public repo name is
  unchanged (only the in-tree product identity reads chessckers).
- **GORM driver → SQLite** (`src/db/db.go`): `dialects/postgres` →
  `dialects/sqlite`; DSN `file:<db>?_busy_timeout=5000&_journal_mode=WAL&_foreign_keys=on`
  (WAL + busy timeout let several clients upload concurrently).
- **Postgres-only SQL → SQLite** (`main.go`):
  - The two atomic counters used data-modifying CTEs
    (`WITH updated AS (UPDATE … RETURNING …) SELECT …`), which SQLite can't run
    (and the bundled SQLite predates `UPDATE … RETURNING`). Replaced with
    `nextRunSeq()`, a one-transaction increment-then-read (SQLite serializes
    writers, so it's atomic).
  - The active-users query used `SPLIT_PART(…)::INTEGER`, aggregate `FILTER`, and
    `now() - INTERVAL '1 day'`. Rewrote to `MAX(engine_version)`,
    `SUM(CASE WHEN … THEN 1 ELSE 0 END)`, and `datetime('now','-1 day')`.
  - `getTopUsers` reads `games_all` / `games_month`, which the Postgres deployment
    created out-of-band. `SetupDB()` now `CREATE VIEW IF NOT EXISTS`es them.
- **Version gate relaxed** (`checkEngineVersion`): the engine reports `0.33.0-dev`;
  lc0's public-fleet policy hard-rejected `-dev`. For an in-house single engine we
  accept any parseable version ≥ `MinEngineVersion` (`v0.0.0` in config).
- **First net auto-bootstraps as best** (`uploadNetwork`): with no current best,
  the upload now sets itself as the run's best network (instead of asking for a
  manual step) so clients immediately have something to self-play.
- **PGN → opaque chessckers movelog.** Chessckers has no standard chess PGN; the
  client uploads a plain movelog (engine move strings + result), stored verbatim.
  Removed the chess `e.p.` annotation strip; `templates/game.tmpl` now shows the
  movelog as text instead of the (already-broken, asset-less) pgn4web chessboard.
- **Network format `.bin`.** Stored/served identically (opaque, sha256 of the
  decompressed upload); the download filename hint is now `.bin.gz`. `Layers`/
  `Filters` columns are retained but cosmetic (lc0 protobuf topology, N/A here).
- **Rebrand + privacy** (`templates/base.tmpl`): title/brand → akshay-chessckers-0,
  removed the lc0 Google-Analytics beacon and dead lczero.org external links.
- **Removed chess/Postgres-era tooling** not used by the loop: `cmd/{compact_games,
  compact_pgns,populate_elo,prepare_match_pgns,refresh_db,tweaks}` and
  `scripts/*.py` (python-chess / ordo / syzygy adjudication), plus the stale
  `main_test.go` (called the old `db.Init(false)` and cross-imported the client).
  Only `cmd/bootstrap` remains (seeds run #1 + train/match params).

## Build & run (Tailscale fleet)

Three+ foreground tabs, each owning its terminal (Ctrl-C stops that piece):

```
# tab 1 — server (this repo), on the tailnet host (e.g. macbookprom1pro)
scripts/launch_server.sh

# tab 2 — trainer bridge (this repo, same host as the server)
scripts/launch_trainer.sh

# tab 3 — a self-play client (lczero-client repo), anywhere on the tailnet
SERVER=http://macbookprom1pro:9830 scripts/launch_client.sh

# tab 4 (optional) — a client on leena over Tailscale (lczero-client repo)
scripts/launch_leena.sh
```

`launch_server.sh` builds + bootstraps + runs the server (needs a C compiler for
the CGO SQLite driver; `CGO_ENABLED=1`). Runtime artifacts (`chessckers.db*`,
`networks/`, `games/`, `pgns/`, built binaries) are git-ignored.

## The trainer bridge (`trainer/trainer_bridge.py`)

lczero keeps training out of the server; so do we. The chessckers engine already
has a continuous trainer (`chessckers_engine.train_continuous`) that watches a
`buffer/` of ccz1 chunks, trains nonstop, and publishes a C++-loadable
`weights.bin`. The bridge (stdlib-only, no torch) spawns it and:

1. **feeds** new `games/run1/training.N.gz` chunks into `<run>/buffer/` (the
   trainer drains + deletes them; the server keeps the originals; a persisted
   high-water mark avoids re-feeding),
2. **uploads** each freshly published `weights.bin` (gzipped) to `/upload_network`,
   which sha256s the decompressed bytes, stores it, and promotes it via a match.

train_continuous publishes an initial random-init net on startup, so the first
upload seeds the run; from then the loop self-sustains:
`dispatch → play → upload_game → feed → train → weights.bin → upload_network →
promote → dispatch the better net`.

> **Net architecture must be v2 (16 planes).** The engine encodes 16 position
> planes (the v2 representation); a v1 net has a **15-channel** input conv, so the
> engine reads 16 planes into a 15-channel weight and **SIGTRAPs** on the first
> eval. The bridge therefore defaults `train_continuous` to `--arch-version v2`
> (`ARCH_VERSION` / `TF_BLOCKS` env in `launch_trainer.sh` to tune). Do not train
> v1 — its nets are not engine-loadable.

## Verified

`go build` + `go vet` clean. End-to-end on SQLite (curl): `upload_network` (first
net auto-set best), `next_game` (train **and** match dispatch with the right
slice token), `upload_game` (chunk + movelog stored, atomic game-number),
`get_network` (302 → cached file, bytes identical to upload), and every web page
(`/`, `/networks`, `/matches`, `/stats`, `/active_users`, `/training_runs`) 200s.
The bridge's feed + multipart net upload verified against the running server.

## Not ported (out of scope / cosmetic)

- Deeper template rebranding (index.tmpl marketing copy) — renders fine as-is.
- A 10×10 stacked-board game viewer (game pages show the movelog text).
- Match adjudication via ordo/syzygy/python-chess (the server's built-in Elo
  promotion is used instead).
- Server integration tests (the old `main_test.go` was removed; tests need
  re-porting against the SQLite schema).
