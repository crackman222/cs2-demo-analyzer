"""
reconstruct_state.py

Stage 4 of cs2_match_analytics_architecture.md: replay each decoded
match's rounds in DuckDB into per-round match_state snapshots
(DuckDBLoader/schema.sql's `match_states` table), one row per
(match, round, clan) rather than a single mutable "current" row — the
whole match is already known, so this is a batch pass over
DuckDBLoader's output, not something updated incrementally as a live
system would.

Semantics of each match_states column (see schema.sql for the same
notes):
    score  — cumulative rounds won through this round, inclusive.
    money  — this clan's summed player balance at this round's
             freeze_end (money entering this round — same sample point
             as player_round_stats.balance).
    alive  — this clan's survivors at this round's conclusion.
    bomb_state — left NULL; DemoDecoder doesn't parse bomb events yet.

Usage:
    python reconstruct_state.py [--db PATH] [--match-id ID] [--overwrite]

Requires: duckdb (pip install duckdb)
"""

import argparse
import sys
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).parent.parent / "DuckDBLoader" / "schema.sql"


def matches_to_process(con: duckdb.DuckDBPyConnection, match_id: str | None, overwrite: bool) -> list[str]:
    if match_id:
        rows = con.execute("SELECT match_id FROM matches WHERE match_id = ?", [match_id]).fetchall()
        if not rows:
            sys.exit(f"No such match in the database: {match_id}")
    else:
        rows = con.execute("SELECT match_id FROM matches ORDER BY match_id").fetchall()

    all_ids = [r[0] for r in rows]
    if overwrite:
        return all_ids

    already_done = {r[0] for r in con.execute("SELECT DISTINCT match_id FROM match_states").fetchall()}
    return [m for m in all_ids if m not in already_done]


def reconstruct_match(con: duckdb.DuckDBPyConnection, match_id: str):
    con.execute("DELETE FROM match_states WHERE match_id = ?", [match_id])

    # round_clan: every (round, clan) pair for this match — the backbone
    # the other three signals get left-joined onto, so a clan with e.g.
    # zero deaths in a round still gets a row instead of being dropped.
    con.execute(f"""
        INSERT INTO match_states (match_id, round, clan_name, score, money, alive)
        WITH round_clan AS (
            SELECT r.match_id, r.round, te.clan_name
            FROM rounds r
            JOIN teams te ON te.match_id = r.match_id
            WHERE r.match_id = ?
        ),
        round_score AS (
            SELECT rc.match_id, rc.round, rc.clan_name,
                   SUM(CASE WHEN rc.clan_name = ro.winner_clan THEN 1 ELSE 0 END)
                       OVER (PARTITION BY rc.match_id, rc.clan_name ORDER BY rc.round
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS score
            FROM round_clan rc
            JOIN rounds ro ON ro.match_id = rc.match_id AND ro.round = rc.round
        ),
        round_money AS (
            SELECT match_id, round, team AS clan_name, SUM(balance) AS money
            FROM player_round_stats
            WHERE match_id = ?
            GROUP BY match_id, round, team
        ),
        round_deaths AS (
            SELECT match_id, round, json_extract_string(target, '$.team') AS clan_name,
                   count(*) AS deaths
            FROM raw_events
            WHERE match_id = ? AND event_type = 'kill'
            GROUP BY match_id, round, json_extract_string(target, '$.team')
        )
        SELECT rc.match_id, rc.round, rc.clan_name,
               rs.score,
               COALESCE(rm.money, 0) AS money,
               GREATEST(0, 5 - COALESCE(rd.deaths, 0)) AS alive
        FROM round_clan rc
        JOIN round_score rs USING (match_id, round, clan_name)
        LEFT JOIN round_money rm USING (match_id, round, clan_name)
        LEFT JOIN round_deaths rd USING (match_id, round, clan_name)
    """, [match_id, match_id, match_id])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", type=Path, default=Path("cs2_matches.duckdb"), help="DuckDB database file (default: ./cs2_matches.duckdb)")
    parser.add_argument("--match-id", help="Reconstruct only this match (default: every match in the database)")
    parser.add_argument("--overwrite", action="store_true", help="Reconstruct matches that already have match_states rows")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"Database not found: {args.db} (run DuckDBLoader/load_matches.py first)")

    con = duckdb.connect(str(args.db))
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))  # idempotent, ensures match_states exists

    targets = matches_to_process(con, args.match_id, args.overwrite)
    if not targets:
        print("Nothing to do — every match already has match_states (use --overwrite to redo).")
        con.close()
        return

    print(f"Reconstructing state for {len(targets)} match(es).")
    for match_id in targets:
        reconstruct_match(con, match_id)
        print(f"Reconstructed {match_id}")

    con.close()
    print(f"Done. Reconstructed {len(targets)} match(es) into {args.db}")


if __name__ == "__main__":
    main()
