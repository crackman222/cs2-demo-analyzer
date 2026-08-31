"""
load_matches.py

Stage 3 of cs2_match_analytics_architecture.md: load DemoDecoder's
per-match JSON output into DuckDB (schema.sql), so later stages (state
reconstruction, insights, dashboard, historical analytics) have something
queryable to read from instead of walking JSON files directly.

Usage:
    python load_matches.py <decoded_root> [--db PATH] [--overwrite]

    <decoded_root> is searched recursively for match folders (anything
    containing a match_summary.json) — matches the nested layout already
    in use (Data\\<event>\\Playoffs\\Finals\\<match_id>\\...).

Requires: duckdb (pip install duckdb)
"""

import argparse
import json
import sys
from pathlib import Path

import duckdb

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Tables touched by this loader, children first (for --overwrite deletes).
LOADED_TABLES = [
    "player_round_stats", "rounds", "raw_events", "players", "teams", "matches",
]


def find_match_dirs(decoded_root: Path) -> list[Path]:
    """Any directory containing a match_summary.json is a decoded match,
    regardless of how deeply nested (event/stage/bracket folders above it
    are just organizational, per the layout already in use under Data\\)."""
    return sorted(p.parent for p in decoded_root.rglob("match_summary.json"))


def sql_path(path: Path) -> str:
    """A filesystem path as a safely-quoted DuckDB string literal. Table
    functions like read_json_auto() take a constant path argument, not a
    bound `?` parameter, so this has to be inlined into the SQL text."""
    return "'" + str(path).replace("\\", "/").replace("'", "''") + "'"


def already_loaded(con: duckdb.DuckDBPyConnection, match_id: str) -> bool:
    return con.execute("SELECT 1 FROM matches WHERE match_id = ?", [match_id]).fetchone() is not None


def delete_match(con: duckdb.DuckDBPyConnection, match_id: str):
    for table in LOADED_TABLES:
        con.execute(f"DELETE FROM {table} WHERE match_id = ?", [match_id])


def load_match(con: duckdb.DuckDBPyConnection, match_dir: Path, summary: dict):
    match_id = summary["match_id"]

    con.execute(
        "INSERT INTO matches (match_id, map, rounds_played) VALUES (?, ?, ?)",
        [match_id, summary.get("map"), summary.get("rounds_played")],
    )

    for team in summary.get("teams", []):
        clan_name = team.get("clan_name")
        con.execute(
            "INSERT INTO teams (match_id, clan_name, score) VALUES (?, ?, ?)",
            [match_id, clan_name, summary.get("score", {}).get(clan_name)],
        )
        for player in team.get("roster", []):
            con.execute(
                "INSERT INTO players (match_id, player_id, name, clan_name) VALUES (?, ?, ?, ?)",
                [match_id, player.get("id"), player.get("name"), clan_name],
            )

    # rounds.json / player_round_stats.json are flat JSON arrays; raw_events.jsonl
    # is newline-delimited. DuckDB reads all three directly via its JSON reader —
    # no manual row-building needed, unlike the irregular match_summary shape above.
    rounds_path = match_dir / "rounds.json"
    if rounds_path.exists():
        con.execute(f"""
            INSERT INTO rounds BY NAME
            SELECT match_id, round, tick, winner, winner_clan, reason, to_json(payload) AS payload
            FROM read_json_auto({sql_path(rounds_path)})
        """)

    stats_path = match_dir / "player_round_stats.json"
    if stats_path.exists():
        con.execute(f"""
            INSERT INTO player_round_stats BY NAME
            SELECT match_id, round, player_id, player_name, team, kills, deaths, assists,
                   damage_dealt, balance, equip_value, round_start_equip_value, cash_spent
            FROM read_json_auto({sql_path(stats_path)})
        """)

    events_path = match_dir / "raw_events.jsonl"
    if events_path.exists():
        con.execute(f"""
            INSERT INTO raw_events BY NAME
            SELECT event_id, match_id, tick, round, event_type,
                   to_json(player) AS player, to_json(target) AS target,
                   weapon, to_json(payload) AS payload
            FROM read_ndjson_auto({sql_path(events_path)})
        """)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("decoded_root", type=Path, help="Root directory to search for decoded match folders")
    parser.add_argument("--db", type=Path, default=Path("cs2_matches.duckdb"), help="DuckDB database file (default: ./cs2_matches.duckdb)")
    parser.add_argument("--overwrite", action="store_true", help="Reload matches that are already in the database")
    args = parser.parse_args()

    if not args.decoded_root.exists():
        sys.exit(f"Path does not exist: {args.decoded_root}")

    match_dirs = find_match_dirs(args.decoded_root)
    if not match_dirs:
        sys.exit(f"No match_summary.json found under {args.decoded_root}")

    print(f"Found {len(match_dirs)} decoded match(es).")

    con = duckdb.connect(str(args.db))
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))

    loaded_count = 0
    for match_dir in match_dirs:
        summary = json.loads((match_dir / "match_summary.json").read_text(encoding="utf-8"))
        match_id = summary["match_id"]

        if already_loaded(con, match_id):
            if not args.overwrite:
                print(f"Skipping {match_id}: already loaded (use --overwrite to reload)")
                continue
            delete_match(con, match_id)

        load_match(con, match_dir, summary)
        print(f"Loaded {match_id}")
        loaded_count += 1

    con.close()
    print(f"Done. Loaded {loaded_count}/{len(match_dirs)} match(es) into {args.db}")


if __name__ == "__main__":
    main()
