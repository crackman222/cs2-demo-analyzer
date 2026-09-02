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
- `--db` — DuckDB database file. Defaults to `cs2_matches.duckdb` next to
  this script's own repo location — works from any working directory.
- `--overwrite` — reload a match that's already in the database (deletes
  its rows from every table first, then reinserts — idempotent). This
  also clears that match's rows in `match_states`/`team_metrics`/
  `player_metrics`/`insights` if any exist (they hold a foreign key on
  `matches`, so they have to go before `matches` can be re-inserted) —
  re-run `StateReconstructor`/`InsightEngine`/`EventDetector` afterward
  if you want that data back. The script prints a note when this happens
  so it's not a silent data loss.

Example, from anywhere:

```
python DuckDBLoader/load_matches.py "Data/Blast Bounty 2026"
```

(`decoded_root` is still relative to wherever you run the command from —
only `--db`'s default is location-independent.)

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
to them (though `--overwrite` can clear rows in the first four, see above).

Every `raw_events`/`rounds` row also keeps its untouched `payload` JSON
column, per Section 4 — nothing is lost if a normalized field turns out
to need re-deriving later.

`matches` also carries `pandascore_match_id`/`tournament`/`league`/
`event_date` — not from the demo (it carries none of these; see
`DemoDecoder/README.md`) but auto-filled from `pending_downloads` (see
below) when unambiguous.

## Tournament/league/date auto-linking

`Dashboard`'s Discover view can **queue** a PandaScore match the user
intends to download (`POST /api/discover/queue`, a direct DuckDB write —
into `pending_downloads`: `pandascore_match_id`, `team_a`, `team_b`,
`event_date`, `tournament`, `league`). After a match is loaded here,
`try_auto_link()` fuzzy-matches its two clan names (case/whitespace-
normalized, substring-tolerant, e.g. `"Spirit"` matches `"Team Spirit"`)
against unlinked `pending_downloads` rows. Exactly one candidate → that
match's `tournament`/`league`/`event_date` get filled in. Zero or
multiple candidates → left blank, no guessing.

No "consumed" flag on `pending_downloads` — a PandaScore match is a whole
**series** (BO3/BO5); a decoded match here is one **map** — so several
`matches` rows are expected to link to the *same* queued entry (verified:
all 4 of this project's existing Spirit-vs-MOUZ maps correctly linked to
one queued entry when this was tested). "Which queued matches are still
unfulfilled" is a query (`Dashboard`'s `GET /api/pending`), not a stored
flag that only the first-linked map would ever get to set.

## Notes

- `rounds.json` and `player_round_stats.json` are loaded straight via
  DuckDB's own JSON reader (`read_json_auto`) with `INSERT ... BY NAME`;
  `raw_events.jsonl` the same way via `read_ndjson_auto`. No manual
  Python row-building for those three — only `match_summary.json`'s
  nested `teams`/`score` shape needs explicit unpacking in Python.
- Dedup is by `match_id` in the `matches` table, checked before each
  match is loaded (mirrors `decode_demos.py`'s own `--overwrite`
  convention at the next layer up).
