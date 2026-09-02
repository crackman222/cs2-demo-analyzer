# EventDetector

Stage 6 of `cs2_match_analytics_architecture.md`: Section 8.4's
turning-point detection, on top of `StateReconstructor`/`InsightEngine`'s
output. Writes into the same `insights` table `InsightEngine` (Stage 5)
uses — this script only ever touches its own 4 `insight_type`s on
re-run, never Stage 5's `economic_pressure`/`momentum_shift` rows.

## Install

```
pip install -r requirements.txt
```

## Usage

```
python detect_events.py [--db PATH] [--match-id ID] [--overwrite]
```

`--db` defaults to `DuckDBLoader/cs2_matches.duckdb` next to this
script's own repo location — works from any working directory.

Requires `DuckDBLoader`, `StateReconstructor`, and `InsightEngine` to have
already run for a match.

## What gets detected

Section 8.4 lists 7 possible classifications; this implements 4 — a first
pass, not all of them:

- **`round_streak`** — a clan wins ≥3 rounds in a row. Fires on every
  round the streak holds (3, 4, 5, ...), so the highest `round_streak`
  value per clan per match is the one that matters if you only want the
  peak.
- **`opening_duel_streak`** — same idea, for winning the round's first
  kill 3+ times in a row.
- **`comeback`** — a clan that was trailing by ≥4 rounds cuts (or
  overturns) that deficit via a `round_streak`. Matches Section 8.4's own
  example exactly: a -6 deficit cut to -1 via a 5-round win streak.
- **`collapse`** — the mirror of a `comeback`, recorded against the
  *other* clan (the one that had the lead and is now giving it up).

**Not yet implemented**: economic swing, player takeover — left for a
later pass.

## Notes

- `STREAK_THRESHOLD` (3) and `COMEBACK_DEFICIT` (4) are explicit,
  documented first-pass thresholds, isolated at the top of
  `detect_events.py` to retune later — same spirit as `InsightEngine`'s
  `classify_buy()` and momentum weights.
- Dedup checks for any of this script's own 4 insight_types already
  present for a match; `--overwrite` deletes only those 4 types before
  redoing, leaving Stage 5's insights alone.
