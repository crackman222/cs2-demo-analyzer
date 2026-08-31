# DuckDBLoader

Stage 3 of `cs2_match_analytics_architecture.md`: loads `DemoDecoder`'s
per-match JSON output into a DuckDB database — embedded, no server to run
(`pip install duckdb`), well suited to this project's aggregation-heavy
queries (momentum/economy/historical analytics across matches).

## Install

```
pip install -r requirements.txt
```

## Usage

```
python load_matches.py <decoded_root> [--db PATH] [--overwrite]
```

- `decoded_root` — searched recursively for match folders (anything
  containing a `match_summary.json`) — matches the nested layout already
  in use, e.g. `Data\Blast Bounty 2026\Playoffs\Finals\<match_id>\`.
- `--db` — DuckDB database file. Default `./cs2_matches.duckdb` (run
  from the repo root, or pass an explicit path).
- `--overwrite` — reload a match that's already in the database (deletes
  its rows from every table first, then reinserts — idempotent).

Example, from the repo root:

```
python DuckDBLoader/load_matches.py "Data/Blast Bounty 2026"
```

## Schema

`schema.sql` creates every table from Section 16 of the architecture doc.
This loader populates:

- `matches`, `teams`, `players` — from `match_summary.json`.
- `raw_events` — from `raw_events.jsonl`. `player`/`target` are stored as
  DuckDB `JSON` columns (mirroring Section 3's normalized event shape)
  rather than flattened into separate id/name/team columns.
- `rounds` — from `rounds.json`.
- `player_round_stats` — from `player_round_stats.json`, including the
  per-round money fields (`balance`, `equip_value`,
  `round_start_equip_value`, `cash_spent`).

`match_states`, `team_metrics`, `player_metrics`, `insights`, and
`predictions` are created empty and reserved for later stages (state
reconstruction, insight engine, prediction) — this loader doesn't write
to them.

Every `raw_events`/`rounds` row also keeps its untouched `payload` JSON
column, per Section 4 — nothing is lost if a normalized field turns out
to need re-deriving later.

## Notes

- `rounds.json` and `player_round_stats.json` are loaded straight via
  DuckDB's own JSON reader (`read_json_auto`) with `INSERT ... BY NAME`;
  `raw_events.jsonl` the same way via `read_ndjson_auto`. No manual
  Python row-building for those three — only `match_summary.json`'s
  nested `teams`/`score` shape needs explicit unpacking in Python.
- Dedup is by `match_id` in the `matches` table, checked before each
  match is loaded (mirrors `decode_demos.py`'s own `--overwrite`
  convention at the next layer up).
