"""
detect_events.py

Stage 6 of cs2_match_analytics_architecture.md: Section 8.4's
turning-point detection, on top of what StateReconstructor/InsightEngine
already put in DuckDB. Writes more rows into the same `insights` table
InsightEngine (Stage 5) uses, alongside its 'economic_pressure' and
'momentum_shift' types — this script only touches its own insight_types
on re-run, never Stage 5's.

Implements 4 of Section 8.4's 7 listed classifications — a first pass,
not all of them (Section 8's own guidance: focus on a small number of
meaningful insights rather than calculating everything):

    round_streak        — a clan wins >= STREAK_THRESHOLD rounds in a row
    opening_duel_streak  — a clan wins >= STREAK_THRESHOLD opening duels in a row
    comeback             — a clan trailing by >= COMEBACK_DEFICIT rounds cuts
                            (or overturns) that deficit via a round_streak
    collapse              — the mirror of a comeback, from the other clan's side

Not yet implemented: economic swing, player takeover.

Usage:
    python detect_events.py [--db PATH] [--match-id ID] [--overwrite]

Requires: duckdb (pip install duckdb)
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).parent.parent / "DuckDBLoader" / "schema.sql"
DB_DEFAULT = Path(__file__).parent.parent / "DuckDBLoader" / "cs2_matches.duckdb"

STREAK_THRESHOLD = 3      # consecutive round/opening-duel wins to count as a streak
COMEBACK_DEFICIT = 4      # rounds a clan must have been trailing by before a
                          # streak, for that streak to count as a comeback
EVENT_TYPES = ("round_streak", "opening_duel_streak", "comeback", "collapse")


def match_round_facts(con: duckdb.DuckDBPyConnection, match_id: str) -> dict:
    """{clan_name: [{'round', 'score', 'opp_score', 'won'}, ...]} ordered by round."""
    rows = con.execute("""
        SELECT ms.round, ms.clan_name, ms.score, opp.score AS opp_score,
               CASE WHEN ro.winner_clan = ms.clan_name THEN 1 ELSE 0 END AS won
        FROM match_states ms
        JOIN rounds ro ON ro.match_id = ms.match_id AND ro.round = ms.round
        JOIN match_states opp ON opp.match_id = ms.match_id AND opp.round = ms.round
             AND opp.clan_name != ms.clan_name
        WHERE ms.match_id = ?
        ORDER BY ms.clan_name, ms.round
    """, [match_id]).fetchall()
    by_clan = defaultdict(list)
    for round_, clan, score, opp_score, won in rows:
        by_clan[clan].append({"round": round_, "score": score, "opp_score": opp_score, "won": won})
    return by_clan


def opening_duel_winners(con: duckdb.DuckDBPyConnection, match_id: str) -> dict:
    """{round: winning clan_name}, from raw_events' first kill of each round."""
    rows = con.execute("""
        WITH ranked AS (
            SELECT round, tick, json_extract_string(player, '$.team') AS attacker_clan,
                   ROW_NUMBER() OVER (PARTITION BY round ORDER BY tick) AS rn
            FROM raw_events
            WHERE match_id = ? AND event_type = 'kill'
        )
        SELECT round, attacker_clan FROM ranked WHERE rn = 1
    """, [match_id]).fetchall()
    return {round_: clan for round_, clan in rows}


def make_insight(match_id, round_, insight_type, clan_name, confidence, data):
    return {
        "id": f"{match_id}-{insight_type}-{round_}-{clan_name}",
        "match_id": match_id, "round": round_, "insight_type": insight_type,
        "clan_name": clan_name, "confidence": confidence, "data": data,
    }


def detect_round_streaks_and_comebacks(match_id: str, by_clan: dict) -> list:
    insights = []
    clans = list(by_clan.keys())

    for clan in clans:
        opponent = next((c for c in clans if c != clan), None)
        opp_rows = {r["round"]: r for r in by_clan.get(opponent, [])} if opponent else {}

        streak = 0
        for r in by_clan[clan]:
            streak = streak + 1 if r["won"] else 0

            if streak >= STREAK_THRESHOLD:
                insights.append(make_insight(
                    match_id, r["round"], "round_streak", clan, 0.75,
                    {"round_streak": streak},
                ))

                start_round = r["round"] - streak
                deficit_before = 0 if start_round < 1 else next(
                    (row["score"] - row["opp_score"] for row in by_clan[clan] if row["round"] == start_round), 0,
                )
                deficit_now = r["score"] - r["opp_score"]

                if deficit_before <= -COMEBACK_DEFICIT and deficit_now > deficit_before:
                    insights.append(make_insight(
                        match_id, r["round"], "comeback", clan, 0.7,
                        {"previous_deficit": deficit_before, "current_deficit": deficit_now, "round_streak": streak},
                    ))
                    if opponent:
                        insights.append(make_insight(
                            match_id, r["round"], "collapse", opponent, 0.7,
                            {"previous_lead": -deficit_before, "current_lead": -deficit_now, "rounds_lost_consecutively": streak},
                        ))
    return insights


def detect_opening_duel_streaks(match_id: str, by_clan: dict, openers: dict) -> list:
    insights = []
    for clan, rows in by_clan.items():
        streak = 0
        for r in rows:
            streak = streak + 1 if openers.get(r["round"]) == clan else 0
            if streak >= STREAK_THRESHOLD:
                insights.append(make_insight(
                    match_id, r["round"], "opening_duel_streak", clan, 0.6,
                    {"round_streak": streak},
                ))
    return insights


def detect_events(con: duckdb.DuckDBPyConnection, match_id: str):
    by_clan = match_round_facts(con, match_id)
    openers = opening_duel_winners(con, match_id)

    all_insights = (
        detect_round_streaks_and_comebacks(match_id, by_clan)
        + detect_opening_duel_streaks(match_id, by_clan, openers)
    )
    for ins in all_insights:
        con.execute(
            "INSERT INTO insights (id, match_id, round, insight_type, clan_name, confidence, data) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [ins["id"], ins["match_id"], ins["round"], ins["insight_type"], ins["clan_name"],
             ins["confidence"], json.dumps(ins["data"])],
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def already_computed(con: duckdb.DuckDBPyConnection, match_id: str) -> bool:
    placeholders = ",".join("?" * len(EVENT_TYPES))
    return con.execute(
        f"SELECT 1 FROM insights WHERE match_id = ? AND insight_type IN ({placeholders}) LIMIT 1",
        [match_id, *EVENT_TYPES],
    ).fetchone() is not None


def clear_match(con: duckdb.DuckDBPyConnection, match_id: str):
    # Only this script's own insight_types -- InsightEngine's
    # economic_pressure/momentum_shift rows for this match are untouched.
    placeholders = ",".join("?" * len(EVENT_TYPES))
    con.execute(f"DELETE FROM insights WHERE match_id = ? AND insight_type IN ({placeholders})", [match_id, *EVENT_TYPES])


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help=f"DuckDB database file (default: {DB_DEFAULT})")
    parser.add_argument("--match-id", help="Detect events for only this match (default: every match without these events yet)")
    parser.add_argument("--overwrite", action="store_true", help="Redo matches that already have these event types")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"Database not found: {args.db} (run DuckDBLoader, StateReconstructor, and InsightEngine first)")

    con = duckdb.connect(str(args.db))
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    if args.match_id:
        if not con.execute("SELECT 1 FROM matches WHERE match_id = ?", [args.match_id]).fetchone():
            sys.exit(f"No such match in the database: {args.match_id}")
        candidates = [args.match_id]
    else:
        candidates = [r[0] for r in con.execute("SELECT match_id FROM matches ORDER BY match_id").fetchall()]

    targets = candidates if args.overwrite else [m for m in candidates if not already_computed(con, m)]
    if not targets:
        print("Nothing to do — every match already has these events (use --overwrite to redo).")
        con.close()
        return

    print(f"Detecting events for {len(targets)} match(es).")
    for match_id in targets:
        clear_match(con, match_id)
        detect_events(con, match_id)
        print(f"Detected events for {match_id}")

    con.close()
    print(f"Done. Processed {len(targets)} match(es) into {args.db}")


if __name__ == "__main__":
    main()
