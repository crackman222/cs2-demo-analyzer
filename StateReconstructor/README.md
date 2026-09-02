# StateReconstructor

Stage 4 of `cs2_match_analytics_architecture.md`: replays each decoded
match's rounds (already loaded by `DuckDBLoader`) into per-round
`match_states` snapshots — one row per `(match, round, clan)`, not a
single mutable "current" row, since the whole match is already known by
the time this runs.

## Install

```
pip install -r requirements.txt
```

## Usage

```
python reconstruct_state.py [--db PATH] [--match-id ID] [--overwrite]
```

- `--db` — DuckDB database file. Defaults to `DuckDBLoader/cs2_matches.duckdb`
  next to this script's own repo location — works from any working
  directory, no need to `cd` anywhere first.
- `--match-id` — reconstruct just one match. Default: every match in
  `matches` that doesn't have `match_states` rows yet.
- `--overwrite` — redo matches that already have `match_states` rows.

Requires `DuckDBLoader/load_matches.py` to have been run first (this
reads `matches`/`teams`/`rounds`/`player_round_stats`/`raw_events`, it
doesn't touch the JSON files directly).

## What each column means

- **`score`** — cumulative rounds won through this round, inclusive
  (matches how round-by-round score is usually shown: "Round 3: 2–1"
  means the score *after* round 3).
- **`money`** — this clan's summed player `balance` at this round's
  freeze_end — money entering this round, the same sample point as
  `player_round_stats.balance`.
- **`alive`** — this clan's survivors when this round ended. An outcome
  stat, not a mid-round live value — a single per-round row can't
  capture alive count changing *within* a round (that would need
  event-level granularity, which raw_events already has if a future
  stage wants finer resolution).
- **`bomb_state`** — left `NULL`. `DemoDecoder` doesn't parse bomb
  events yet; adding that is a `DemoDecoder` change, not this stage's.

`alive` assumes 5 real players per clan (`5 - deaths_this_round`) — a
stand-in/disconnect situation would throw this off; not handled.
