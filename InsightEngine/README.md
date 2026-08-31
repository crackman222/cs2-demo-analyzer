# InsightEngine

Stage 5 of `cs2_match_analytics_architecture.md`: the first three
"Initial Insights" from Section 8 — momentum, economic pressure, and
player performance — computed from what `StateReconstructor` /
`DuckDBLoader` already put in DuckDB. (Turning-point detection, Section
8.4, is Stage 6 — a separate script.)

## Install

```
pip install -r requirements.txt
```

## Usage

```
python compute_insights.py [--db PATH] [--match-id ID] [--overwrite]
```

Requires `DuckDBLoader/load_matches.py` and
`StateReconstructor/reconstruct_state.py` to have already run for a
match — this reads `rounds`/`player_round_stats`/`raw_events`/`teams`,
not the JSON files directly.

## What gets computed

All formulas here are explicit, first-pass, documented designs — Section
8 itself says the weights/scoring "should be experimental" / "designed
by the project." None of this claims to reproduce HLTV's or any other
site's proprietary rating.

### `team_metrics` — per (match, round, clan)

- **`equip_value`** — summed player equipment value that round (from
  `player_round_stats`).
- **`momentum`** (0–100) — equal-weighted average of four components over
  a trailing 5-round window: round win rate, kill differential (scaled),
  opening-duel win rate, and relative economic advantage vs the opponent
  over the same window.

### `player_metrics` — per (match, player), `round = NULL`

These are whole-match aggregates, not per-round values (the `round`
column exists for future per-round metrics; match-level ones use `NULL`
to distinguish them):

`kills`, `deaths`, `assists`, `damage_dealt`, `kd_ratio`, `adr`,
`opening_kills`, `opening_deaths`, and a composite `rating` (50% K/D +
30% ADR + 20% opening-duel rate, each capped before blending so no single
stat dominates). Not yet included: clutches, recent-form, round-impact —
Section 8.3 lists these too; left for a later pass.

### `insights`

- **`economic_pressure`** — a clan enters a round on a ≥2-round losing
  streak with a non-full buy (`classify_buy()`: team equip value <
  $10k eco, <$20k force, else full — an explicit threshold, not a
  precise reproduction of any site's buy classifier).
- **`momentum_shift`** — a clan's momentum score swings ≥20 points
  (of 100) between consecutive rounds.

## Notes

- Buy classification and momentum weights are the two places most worth
  tuning later — both are isolated in `classify_buy()` and
  `compute_momentum_and_insights()`'s component weights, deliberately
  kept simple to adjust.
- Dedup is by `match_id` (checked via a `team_metrics` row with
  `metric = 'momentum'`), same `--overwrite` convention as the other
  stages.
