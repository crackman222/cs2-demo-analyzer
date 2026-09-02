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
DB_DEFAULT = Path(__file__).parent / "cs2_matches.duckdb"

# Every table with a FK on matches(match_id) — children first, matches
# last — so --overwrite's delete-then-reinsert doesn't hit a foreign key
# violation. This loader only populates the first six; the rest
# (match_states/team_metrics/player_metrics/insights/predictions) belong
# to later stages, but still reference matches and so still have to be
# cleared before matches itself can be deleted. --overwrite therefore
# clears those stages' data too — re-run StateReconstructor/InsightEngine/
# EventDetector afterward if you want it back (this loader can't
# regenerate it itself, it doesn't have that logic).
LOADED_TABLES = [
    "predictions", "insights", "player_metrics", "team_metrics", "match_states",
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


def normalize_name(name: str) -> str:
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def names_match(a: str, b: str) -> bool:
    """Loose enough to match "Spirit" against "Team Spirit" (same
    tolerance the Dashboard's team autocomplete already assumes about
    name variants between sources), strict enough that an empty name
    never matches anything."""
    na, nb = normalize_name(a), normalize_name(b)
    return bool(na) and bool(nb) and (na in nb or nb in na)


def try_auto_link(con: duckdb.DuckDBPyConnection, match_id: str, clan_names: list[str]):
    """A queued PandaScore match (Dashboard's Discover 'queue' action) is
    a whole series; this match is one map of it. Link only when exactly
    one pending_downloads entry's two team names unambiguously match this
    match's two clans — no guessing when zero or multiple candidates fit.
    Deliberately no "consumed" marker: several maps from the same series
    are expected to link to the same pending entry."""
    if len(clan_names) != 2:
        return
    clan_a, clan_b = clan_names
    pending = con.execute("SELECT pandascore_match_id, team_a, team_b, event_date, tournament, league FROM pending_downloads").fetchall()

    candidates = [
        row for row in pending
        if (names_match(clan_a, row[1]) and names_match(clan_b, row[2]))
        or (names_match(clan_a, row[2]) and names_match(clan_b, row[1]))
    ]

    if len(candidates) == 1:
        pandascore_id, _, _, event_date, tournament, league = candidates[0]
        con.execute(
            "UPDATE matches SET pandascore_match_id = ?, event_date = ?, tournament = ?, league = ? WHERE match_id = ?",
            [pandascore_id, event_date, tournament, league, match_id],
        )
        print(f"  Linked to pending download {pandascore_id} ({tournament})")
    elif len(candidates) > 1:
        print(f"  Note: {len(candidates)} ambiguous pending-download matches for {match_id} — not auto-linking")


def load_match(con: duckdb.DuckDBPyConnection, match_dir: Path, summary: dict):
    match_id = summary["match_id"]

    con.execute(
        "INSERT INTO matches (match_id, map, rounds_played) VALUES (?, ?, ?)",
        [match_id, summary.get("map"), summary.get("rounds_played")],
    )

    clan_names = []
    for team in summary.get("teams", []):
        clan_name = team.get("clan_name")
        clan_names.append(clan_name)
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

    try_auto_link(con, match_id, clan_names)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("decoded_root", type=Path, help="Root directory to search for decoded match folders")
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help=f"DuckDB database file (default: {DB_DEFAULT})")
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
            downstream = [t for t in ("match_states", "team_metrics", "player_metrics", "insights")
                          if con.execute(f"SELECT 1 FROM {t} WHERE match_id = ? LIMIT 1", [match_id]).fetchone()]
            if downstream:
                print(f"  Note: also clearing {match_id}'s data in {', '.join(downstream)} — "
                      f"re-run those stages afterward if you want it recomputed")
            delete_match(con, match_id)

        load_match(con, match_dir, summary)
        print(f"Loaded {match_id}")
        loaded_count += 1

    con.close()
    print(f"Done. Loaded {loaded_count}/{len(match_dirs)} match(es) into {args.db}")


if __name__ == "__main__":
    main()
