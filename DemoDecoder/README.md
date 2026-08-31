# DemoDecoder

Standalone decoder for CS2 `.dem` files you already have on disk. It does
not talk to HLTV or any other network source — it's the "parse demo /
normalize" half of the pipeline in `cs2_live_match_analytics_architecture.md`
(Appendix B), split out from demo *acquisition* so it can run against demos
however they got downloaded (`HLTVDemoDownloader`, `HLTVDemoDownloader2.0`,
a direct organizer link, manually placed in a folder, ...).

## Install

```
pip install -r requirements.txt
```

`.rar` archives additionally need `patool` (`pip install patool`) plus
7-Zip or WinRAR/unrar available on PATH. Plain `.dem` files and `.zip`
archives need nothing extra.

## Usage

```
python decode_demos.py <input_path> [-o OUTPUT_DIR] [--overwrite]
```

- `input_path` — a single `.dem`/`.rar`/`.zip` file, or a directory to
  search recursively (e.g. a folder of demos left behind by either
  Downloader script).
- `-o/--output-dir` — where per-match output is written. Default `./decoded`.
- `--overwrite` — re-decode a match even if output for it already exists
  (by default already-decoded matches are skipped).

## Output

For each demo, `<output_dir>/<match_id>/` gets:

- `header.json` — the demo header as reported by the parser (map, etc.)
- `match_summary.json` — derived summary: `map`, `teams` (clan name +
  roster), `score` (rounds won per clan), `rounds_played`. Team names and
  score are pulled straight out of the demo itself (`team_clan_name` /
  `team_num` props) — no external API involved.
- `raw_events.jsonl` — one normalized kill event per line (Section 3 schema:
  `event_type`, `round`, `player`, `target`, `weapon`), each also carrying
  the untouched parser row under `payload` (Section 4: raw data stays
  immutable, so normalization can be redone later without re-parsing).
  `player`/`target` include `team` (clan name).
- `rounds.json` — one row per round: side winner (`winner`), the winning
  clan (`winner_clan`), end reason, tick.
- `player_round_stats.json` — kills/deaths/assists/damage (and `team`) per
  player per round, plus money: `balance` (cash on hand), `equip_value`
  (loadout value), `round_start_equip_value` (loadout value carried in
  before this round's buys), `cash_spent` — all sampled at
  `round_freeze_end` (buy phase just closed). One row per connected player
  per round, even if they had no kill/death/damage that round.

These are file-based staging output for the `raw_events` / `rounds` /
`player_round_stats` tables from Section 16 / B.4 of the architecture doc —
load them into Postgres once that ingestion layer exists; this script
doesn't assume any DB connection.

## Notes / caveats

- `match_id` is derived from the demo's filename, since a bare `.dem`
  carries no other match metadata once separated from the page it came
  from. Rename files beforehand if you want a specific match_id.
- Demos have no wall-clock timestamps, only in-demo `tick` — so events are
  stamped with `tick` (and a `round` number derived from `round_end`
  boundaries), not an ISO timestamp. Converting tick → real UTC time would
  need the match's real-world start time from an external source (e.g.
  PandaScore).
- Team names and score come from the demo directly (a player's clan is
  stable all match via `team_clan_name`; their side flips at halftime via
  `team_num`, which is how `winner_clan` is attributed per round) —
  verified against a real demo. What a single map's demo genuinely can't
  carry — series/tournament context, canonical team/player IDs, VOD links —
  is where PandaScore's free fixtures endpoints (`/csgo/matches`, `/teams`)
  still come in, per Appendix B of the architecture doc; not wired up here.
- Uses [`demoparser2`](https://github.com/LaihoE/demoparser) directly
  rather than `awpy` (which wraps the same underlying parser) to keep this
  script's dependency footprint small.
