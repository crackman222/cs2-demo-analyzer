"""
compute_insights.py

Stage 5 of cs2_match_analytics_architecture.md: the first three "Initial
Insights" from Section 8 — momentum (8.1), economic pressure (8.2), and
player performance (8.3) — computed from what StateReconstructor/
DuckDBLoader already put in DuckDB. Turning-point detection (8.4) is
Stage 6, not this script.

All formulas here are explicit, documented, first-pass designs per
Section 8's own guidance ("the exact weights should be experimental" /
"the scoring system should be designed by the project") — not a claim to
reproduce HLTV's or any other site's proprietary rating.

Writes:
    team_metrics    — 'equip_value' and 'momentum' per (match, round, clan)
    player_metrics  — match-level per-player stats (round = NULL; these
                       are whole-match aggregates, not per-round)
    insights        — 'economic_pressure' and 'momentum_shift' events

Usage:
    python compute_insights.py [--db PATH] [--match-id ID] [--overwrite]

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

MOMENTUM_WINDOW = 5          # trailing rounds considered for momentum
LOSS_STREAK_THRESHOLD = 2    # consecutive round losses before flagging economic pressure
MOMENTUM_SHIFT_THRESHOLD = 20  # momentum-point swing (of 100) that counts as a shift


def classify_buy(team_equip_value: float) -> str:
    """Team-total (5-player) equipment value -> eco/force/full. Thresholds
    are an explicit, documented first pass (Section 8.2), not a precise
    reproduction of any site's proprietary buy classifier."""
    if team_equip_value < 10_000:
        return "eco"
    if team_equip_value < 20_000:
        return "force"
    return "full"


# ---------------------------------------------------------------------------
# Economic pressure (8.2) — the equip_value team_metric this and momentum
# both build on
# ---------------------------------------------------------------------------

def compute_equip_value(con: duckdb.DuckDBPyConnection, match_id: str):
    con.execute("""
        INSERT INTO team_metrics (match_id, round, clan_name, metric, value)
        SELECT match_id, round, team, 'equip_value', SUM(equip_value)
        FROM player_round_stats
        WHERE match_id = ?
        GROUP BY match_id, round, team
    """, [match_id])


# ---------------------------------------------------------------------------
# Shared: opening kill/death per round (feeds both player performance and
# momentum's "opening duel performance" component)
# ---------------------------------------------------------------------------

def opening_kills(con: duckdb.DuckDBPyConnection, match_id: str) -> dict:
    """{round: {attacker_id, attacker_clan, victim_id, victim_clan}} — the
    first kill (by tick) of each round."""
    rows = con.execute("""
        WITH ranked AS (
            SELECT round, tick,
                   json_extract_string(player, '$.id') AS attacker_id,
                   json_extract_string(player, '$.team') AS attacker_clan,
                   json_extract_string(target, '$.id') AS victim_id,
                   json_extract_string(target, '$.team') AS victim_clan,
                   ROW_NUMBER() OVER (PARTITION BY round ORDER BY tick) AS rn
            FROM raw_events
            WHERE match_id = ? AND event_type = 'kill'
        )
        SELECT round, attacker_id, attacker_clan, victim_id, victim_clan
        FROM ranked WHERE rn = 1
    """, [match_id]).fetchall()
    return {
        r[0]: {"attacker_id": r[1], "attacker_clan": r[2], "victim_id": r[3], "victim_clan": r[4]}
        for r in rows
    }


# ---------------------------------------------------------------------------
# Player performance (8.3)
# ---------------------------------------------------------------------------

def compute_player_metrics(con: duckdb.DuckDBPyConnection, match_id: str, openings: dict):
    rows = con.execute("""
        SELECT player_id, team,
               SUM(kills), SUM(deaths), SUM(assists), SUM(damage_dealt), COUNT(*) AS rounds_played
        FROM player_round_stats
        WHERE match_id = ?
        GROUP BY player_id, team
    """, [match_id]).fetchall()

    opening_kill_counts, opening_death_counts = defaultdict(int), defaultdict(int)
    for o in openings.values():
        if o["attacker_id"]:
            opening_kill_counts[o["attacker_id"]] += 1
        if o["victim_id"]:
            opening_death_counts[o["victim_id"]] += 1

    for player_id, _team, kills, deaths, assists, damage, rounds_played in rows:
        kd = kills / max(deaths, 1)
        adr = damage / rounds_played if rounds_played else 0.0
        ok = opening_kill_counts.get(player_id, 0)
        od = opening_death_counts.get(player_id, 0)
        opening_rate = ok / rounds_played if rounds_played else 0.0

        # First-pass composite rating: kd/adr/opening-duel-rate, equally
        # weighted 50/30/20, each capped at a "good" reference value before
        # blending so one huge stat can't single-handedly max the score.
        # Not clutches/round-impact/recent-form yet (Section 8.3 lists those
        # too) — left for a later refinement pass.
        rating = round(100 * (
            0.5 * min(kd / 1.5, 1.0) +
            0.3 * min(adr / 100, 1.0) +
            0.2 * opening_rate
        ), 1)

        for metric, value in [
            ("kills", kills), ("deaths", deaths), ("assists", assists),
            ("damage_dealt", damage), ("kd_ratio", round(kd, 2)),
            ("adr", round(adr, 1)), ("opening_kills", ok), ("opening_deaths", od),
            ("rating", rating),
        ]:
            con.execute(
                "INSERT INTO player_metrics (match_id, round, player_id, metric, value) VALUES (?, NULL, ?, ?, ?)",
                [match_id, player_id, metric, value],
            )


# ---------------------------------------------------------------------------
# Momentum (8.1) + the insight events derived alongside it
# ---------------------------------------------------------------------------

def compute_momentum_and_insights(con: duckdb.DuckDBPyConnection, match_id: str, openings: dict):
    rows = con.execute("""
        WITH round_clan AS (
            SELECT r.match_id, r.round, te.clan_name
            FROM rounds r JOIN teams te ON te.match_id = r.match_id
            WHERE r.match_id = ?
        ),
        kills_agg AS (
            SELECT match_id, round, team AS clan_name, SUM(kills) AS kills, SUM(deaths) AS deaths
            FROM player_round_stats WHERE match_id = ?
            GROUP BY match_id, round, team
        )
        SELECT rc.round, rc.clan_name,
               CASE WHEN ro.winner_clan = rc.clan_name THEN 1 ELSE 0 END AS won,
               COALESCE(ka.kills, 0) AS kills, COALESCE(ka.deaths, 0) AS deaths,
               COALESCE(tm.value, 0) AS equip_value
        FROM round_clan rc
        JOIN rounds ro ON ro.match_id = rc.match_id AND ro.round = rc.round
        LEFT JOIN kills_agg ka ON ka.match_id = rc.match_id AND ka.round = rc.round AND ka.clan_name = rc.clan_name
        LEFT JOIN team_metrics tm ON tm.match_id = rc.match_id AND tm.round = rc.round
             AND tm.clan_name = rc.clan_name AND tm.metric = 'equip_value'
        ORDER BY rc.clan_name, rc.round
    """, [match_id, match_id]).fetchall()

    by_clan = defaultdict(list)
    for round_, clan, won, kills, deaths, equip in rows:
        by_clan[clan].append({"round": round_, "won": won, "kills": kills, "deaths": deaths, "equip": equip})

    clans = list(by_clan.keys())
    momentum_rows, insight_rows = [], []

    for clan, clan_rounds in by_clan.items():
        opponent = next((c for c in clans if c != clan), None)
        opp_by_round = {r["round"]: r for r in by_clan.get(opponent, [])} if opponent else {}

        prev_momentum = None
        loss_streak = 0
        for i, r in enumerate(clan_rounds):
            window = clan_rounds[max(0, i - MOMENTUM_WINDOW + 1): i + 1]

            recent_perf = sum(w["won"] for w in window) / len(window)

            kd_sum = sum(w["kills"] - w["deaths"] for w in window)
            kill_diff_component = max(0.0, min(1.0, (kd_sum / (5 * len(window)) + 1) / 2))

            opens_won = sum(1 for w in window if openings.get(w["round"], {}).get("attacker_clan") == clan)
            opening_component = opens_won / len(window)

            own_equip = sum(w["equip"] for w in window)
            opp_equip = sum(opp_by_round.get(w["round"], {}).get("equip", 0) for w in window)
            if own_equip + opp_equip > 0:
                econ_component = max(0.0, min(1.0, (own_equip - opp_equip) / (own_equip + opp_equip) / 2 + 0.5))
            else:
                econ_component = 0.5

            # Equal-weighted average of the four Section 8.1 components,
            # scaled to 0-100 — an explicit first pass, not a validated model.
            momentum = round(100 * (recent_perf + kill_diff_component + opening_component + econ_component) / 4, 1)
            momentum_rows.append((match_id, r["round"], clan, "momentum", momentum))

            if prev_momentum is not None and abs(momentum - prev_momentum) >= MOMENTUM_SHIFT_THRESHOLD:
                insight_rows.append({
                    "id": f"{match_id}-momentum_shift-{r['round']}-{clan}",
                    "match_id": match_id, "round": r["round"], "insight_type": "momentum_shift",
                    "clan_name": clan, "confidence": 0.6,
                    "data": {"previous_momentum": prev_momentum, "current_momentum": momentum},
                })
            prev_momentum = momentum

            # Economic pressure (8.2): entering this round on a losing
            # streak with a non-full buy — loss_streak here is rounds *before*
            # this one (checked before updating it with this round's result).
            if loss_streak >= LOSS_STREAK_THRESHOLD:
                buy_type = classify_buy(r["equip"])
                if buy_type in ("eco", "force"):
                    insight_rows.append({
                        "id": f"{match_id}-economic_pressure-{r['round']}-{clan}",
                        "match_id": match_id, "round": r["round"], "insight_type": "economic_pressure",
                        "clan_name": clan, "confidence": 0.7,
                        "data": {"round_streak": loss_streak, "buy_type": buy_type, "equip_value": r["equip"]},
                    })

            loss_streak = 0 if r["won"] else loss_streak + 1

    for row in momentum_rows:
        con.execute(
            "INSERT INTO team_metrics (match_id, round, clan_name, metric, value) VALUES (?, ?, ?, ?, ?)", row,
        )
    for ins in insight_rows:
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
    return con.execute(
        "SELECT 1 FROM team_metrics WHERE match_id = ? AND metric = 'momentum' LIMIT 1", [match_id],
    ).fetchone() is not None


def clear_match(con: duckdb.DuckDBPyConnection, match_id: str):
    for table in ("team_metrics", "player_metrics", "insights"):
        con.execute(f"DELETE FROM {table} WHERE match_id = ?", [match_id])


def process_match(con: duckdb.DuckDBPyConnection, match_id: str):
    compute_equip_value(con, match_id)
    openings = opening_kills(con, match_id)
    compute_player_metrics(con, match_id, openings)
    compute_momentum_and_insights(con, match_id, openings)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help=f"DuckDB database file (default: {DB_DEFAULT})")
    parser.add_argument("--match-id", help="Compute insights for only this match (default: every match without insights yet)")
    parser.add_argument("--overwrite", action="store_true", help="Recompute matches that already have insights")
    args = parser.parse_args()

    if not args.db.exists():
        sys.exit(f"Database not found: {args.db} (run DuckDBLoader/load_matches.py and StateReconstructor/reconstruct_state.py first)")

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
        print("Nothing to do — every match already has insights (use --overwrite to redo).")
        con.close()
        return

    print(f"Computing insights for {len(targets)} match(es).")
    for match_id in targets:
        clear_match(con, match_id)
        process_match(con, match_id)
        print(f"Computed {match_id}")

    con.close()
    print(f"Done. Computed {len(targets)} match(es) into {args.db}")


if __name__ == "__main__":
    main()
