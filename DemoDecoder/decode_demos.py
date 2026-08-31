"""
decode_demos.py

Standalone decoder for CS2 (.dem) demo files you already have on disk —
no HLTV scraping, no network access at all. This is the "Python: parse
demo (awpy / demoparser2)" + "Normalize" step from Appendix B of
cs2_live_match_analytics_architecture.md, split out so it can run against
any pile of demos regardless of how they got downloaded (HLTVDemoDownloader,
HLTVDemoDownloader2.0, a tournament organizer's direct link, manually, ...).

What it does, per demo:
    1. Locate the .dem file (extracting it first if it's still in a
       .rar/.zip archive).
    2. Parse it with demoparser2 (kills, round boundaries, damage, header).
    3. Normalize kills into the internal event schema from Section 3 of the
       architecture doc.
    4. Write out raw_events / rounds / player_round_stats as JSON, matching
       the tables in Section 16 / B.4 — as files, not live Postgres rows,
       since no DB access layer exists in this repo yet. Load these into
       Postgres once that part is built.

Every normalized record also keeps a "payload" field with the untouched
parser row, per Section 4 ("raw data should ... be treated as immutable") —
so if a column-name guess below is wrong or CS2 changes an event's fields,
the original data survives and normalization can be redone later.

Usage:
    python decode_demos.py <input_path> [-o OUTPUT_DIR] [--overwrite]

    <input_path> can be a single .dem/.rar/.zip file or a directory to
    search recursively.

Requires: demoparser2, pandas (pip install demoparser2 pandas)
Optional: patoolib, for extracting .rar archives (pip install patool;
    also needs 7-Zip or WinRAR/unrar available on PATH). Plain .zip
    archives are handled with the standard library, no extra dependency.
"""

import argparse
import bisect
import json
import re
import sys
import zipfile
from pathlib import Path

import pandas as pd
from demoparser2 import DemoParser

DEMO_SUFFIX = ".dem"
ARCHIVE_SUFFIXES = {".rar", ".zip"}

# Source/CS2 engine team-entity constants (fixed by the engine, not per-demo):
# team_num 2 is always the T slot, 3 is always the CT slot. A player's
# team_num flips between these at halftime; round_end's "winner" is already
# a plain "T"/"CT" string, so this maps that side back to a team_num to look
# up which clan currently held it (see resolve_clans()).
WINNER_SIDE_TO_TEAM_NUM = {"T": 2, "CT": 3}


# ---------------------------------------------------------------------------
# Discovery + extraction
# ---------------------------------------------------------------------------

def find_inputs(input_path: Path):
    """Return (demo_files, archive_files) found at/under input_path."""
    if input_path.is_file():
        candidates = [input_path]
    else:
        candidates = list(input_path.rglob("*"))

    demos = [p for p in candidates if p.is_file() and p.suffix.lower() == DEMO_SUFFIX]
    archives = [p for p in candidates if p.is_file() and p.suffix.lower() in ARCHIVE_SUFFIXES]
    return demos, archives


def extract_archive(archive_path: Path, extract_root: Path) -> list[Path]:
    """Extract one archive into extract_root/<archive stem>/ and return any
    .dem files found inside. Skips extraction if that folder already has a
    .dem in it (same "already processed" idea as the rest of the pipeline)."""
    dest = extract_root / archive_path.stem
    existing = list(dest.rglob(f"*{DEMO_SUFFIX}")) if dest.exists() else []
    if existing:
        print(f"Already extracted: {archive_path.name}")
        return existing

    dest.mkdir(parents=True, exist_ok=True)
    print(f"Extracting {archive_path.name} ...")

    if archive_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive_path) as zf:
            zf.extractall(dest)
    else:  # .rar
        try:
            import patoolib
        except ImportError:
            print(
                f"  Skipping {archive_path.name}: patool is not installed "
                f"(pip install patool), needed to extract .rar archives."
            )
            return []
        try:
            patoolib.extract_archive(str(archive_path), outdir=str(dest), verbosity=-1)
        except Exception as exc:
            print(
                f"  Skipping {archive_path.name}: extraction failed ({exc}). "
                f"patool needs 7-Zip or WinRAR/unrar available on PATH."
            )
            return []

    return list(dest.rglob(f"*{DEMO_SUFFIX}"))


def collect_demo_files(input_path: Path, extract_root: Path) -> list[Path]:
    demos, archives = find_inputs(input_path)
    for archive in archives:
        demos.extend(extract_archive(archive, extract_root))
    # De-dupe (an archive could re-extract a demo already found loose) while
    # keeping a stable order.
    seen = set()
    unique_demos = []
    for demo in demos:
        resolved = demo.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_demos.append(demo)
    return unique_demos


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

def match_id_for(demo_path: Path) -> str:
    """Derive a match_id from the filename, since these demos have no
    other match metadata attached once separated from the HLTV page they
    came from."""
    stem = demo_path.stem
    return re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_") or "match"


def safe_parse_event(parser: DemoParser, event_name: str) -> pd.DataFrame:
    """demoparser2's event set/columns can shift between CS2 patches. Treat
    a missing/unparseable event as "no data" rather than a hard failure so
    one broken event type doesn't take down the whole demo."""
    try:
        return parser.parse_event(event_name)
    except Exception as exc:
        print(f"  Note: could not parse '{event_name}' ({exc})")
        return pd.DataFrame()


def round_lookup(round_end_df: pd.DataFrame):
    """Build a function mapping a game tick -> 1-indexed round number, using
    round_end tick boundaries (a kill in round N happens before round N's
    round_end tick)."""
    if round_end_df.empty or "tick" not in round_end_df.columns:
        return lambda tick: None
    ticks = sorted(round_end_df["tick"].tolist())
    return lambda tick: bisect.bisect_left(ticks, tick) + 1


def player_dict(row, prefix: str, player_info: dict):
    name = row.get(f"{prefix}_name")
    steamid = row.get(f"{prefix}_steamid")
    if pd.isna(name) and pd.isna(steamid):
        return None
    steamid_str = None if pd.isna(steamid) else str(int(steamid))
    info = player_info.get(steamid_str, {}) if steamid_str else {}
    return {
        "id": steamid_str,
        "name": None if pd.isna(name) else name,
        "team": info.get("team"),
    }


def resolve_clans(parser: DemoParser, round_end_df: pd.DataFrame):
    """Return (player_info, round_clans).

    player_info: {steamid: {"name": ..., "team": clan_name}} — a player's
    clan is stable for the whole match (team_clan_name), unlike their side.

    round_clans: {tick: {team_num: clan_name}} — which clan held which side
    (team_num 2/3) at each round_end tick, since sides swap at halftime.
    Both are built from the mode of (possibly several, occasionally stray/
    NaN — seen from entity-slot reuse) tick samples per player, not just the
    first value seen, to ride out that noise.
    """
    try:
        all_ticks_df = parser.parse_ticks(["team_clan_name"])
    except Exception as exc:
        print(f"  Note: could not parse team_clan_name ({exc})")
        return {}, {}

    player_info = {}
    if not all_ticks_df.empty:
        for steamid, group in all_ticks_df.dropna(subset=["team_clan_name"]).groupby("steamid"):
            player_info[str(int(steamid))] = {
                "name": group["name"].mode().iat[0] if "name" in group.columns and not group["name"].empty else None,
                "team": group["team_clan_name"].mode().iat[0],
            }

    round_clans = {}
    round_ticks = sorted(round_end_df["tick"].dropna().unique().tolist()) if not round_end_df.empty else []
    if round_ticks:
        try:
            side_df = parser.parse_ticks(["team_clan_name", "team_num"], ticks=round_ticks)
        except Exception as exc:
            print(f"  Note: could not parse per-round team sides ({exc})")
            side_df = pd.DataFrame()
        if not side_df.empty:
            grouped = side_df.dropna(subset=["team_clan_name", "team_num"]).groupby(["tick", "team_num"])["team_clan_name"]
            for (tick, team_num), clans in grouped:
                round_clans.setdefault(int(tick), {})[int(team_num)] = clans.mode().iat[0]

    return player_info, round_clans


def resolve_economy(parser: DemoParser, round_end_df: pd.DataFrame) -> dict:
    """Return {(round_no, steamid_str): {"balance", "equip_value",
    "round_start_equip_value", "cash_spent"}}, sampled at each round's
    round_freeze_end tick (buy phase just closed — the standard "what did
    they buy" moment).

    round_freeze_end ticks line up 1:1, in order, with the real rounds in
    round_end_df (verified against a real demo: both were 21 for 21 real
    rounds, no bogus-first-round artifact on the freeze_end side). If the
    counts don't match here, don't guess a misaligned pairing — skip money
    data for this demo instead.
    """
    if round_end_df.empty:
        return {}

    freeze_df = safe_parse_event(parser, "round_freeze_end")
    freeze_ticks = sorted(freeze_df["tick"].dropna().unique().tolist()) if not freeze_df.empty else []
    if len(freeze_ticks) != len(round_end_df):
        print(
            f"  Note: round_freeze_end count ({len(freeze_ticks)}) doesn't match "
            f"round count ({len(round_end_df)}) — skipping per-round money."
        )
        return {}

    tick_to_round_no = {tick: i + 1 for i, tick in enumerate(freeze_ticks)}

    try:
        money_df = parser.parse_ticks(
            ["balance", "current_equip_value", "round_start_equip_value", "cash_spent_this_round"],
            ticks=freeze_ticks,
        )
    except Exception as exc:
        print(f"  Note: could not parse round economy ({exc})")
        return {}

    economy = {}
    for _, row in money_df.iterrows():
        steamid = row.get("steamid")
        tick = row.get("tick")
        if pd.isna(steamid) or tick not in tick_to_round_no:
            continue
        key = (tick_to_round_no[tick], str(int(steamid)))
        economy[key] = {
            "balance": None if pd.isna(row.get("balance")) else int(row.get("balance")),
            "equip_value": None if pd.isna(row.get("current_equip_value")) else int(row.get("current_equip_value")),
            "round_start_equip_value": None if pd.isna(row.get("round_start_equip_value")) else int(row.get("round_start_equip_value")),
            "cash_spent": None if pd.isna(row.get("cash_spent_this_round")) else int(row.get("cash_spent_this_round")),
        }
    return economy


def clean_payload(row) -> dict:
    """Row -> plain-JSON-serializable dict (numpy/pandas scalar -> Python)."""
    payload = {}
    for key, value in row.items():
        if pd.isna(value):
            payload[key] = None
        elif hasattr(value, "item"):  # numpy scalar
            payload[key] = value.item()
        else:
            payload[key] = value
    return payload


# ---------------------------------------------------------------------------
# Per-demo decode
# ---------------------------------------------------------------------------

def decode_demo(demo_path: Path, match_id: str) -> dict:
    print(f"Parsing {demo_path.name} (match_id={match_id}) ...")
    parser = DemoParser(str(demo_path))

    header = parser.parse_header()
    kills_df = safe_parse_event(parser, "player_death")
    round_end_df = safe_parse_event(parser, "round_end")
    hurt_df = safe_parse_event(parser, "player_hurt")

    # The very first round_end row can be a bogus tick=0/winner=NaN
    # placeholder (seen on a real demo during testing) — drop it before it
    # throws off round numbering and score tallying. reset_index so "round"
    # numbers below stay contiguous after the drop.
    if not round_end_df.empty and "winner" in round_end_df.columns:
        round_end_df = round_end_df[round_end_df["winner"].notna()].reset_index(drop=True)

    tick_to_round = round_lookup(round_end_df)
    player_info, round_clans = resolve_clans(parser, round_end_df)
    economy = resolve_economy(parser, round_end_df)

    # --- raw_events (Section 3 schema, kills) -----------------------------
    raw_events = []
    for i, row in kills_df.iterrows():
        tick = row.get("tick")
        raw_events.append({
            "event_id": f"{match_id}-kill-{i}",
            "match_id": match_id,
            "tick": None if pd.isna(tick) else int(tick),
            "round": tick_to_round(tick) if tick is not None and not pd.isna(tick) else None,
            "event_type": "kill",
            "player": player_dict(row, "attacker", player_info),
            "target": player_dict(row, "user", player_info),
            "weapon": row.get("weapon"),
            "payload": clean_payload(row),
        })

    # --- rounds -------------------------------------------------------------
    rounds = []
    score = {}
    for i, row in round_end_df.iterrows():
        tick = row.get("tick")
        tick_int = None if pd.isna(tick) else int(tick)
        winner = row.get("winner")
        winner_clan = None
        if tick_int is not None and winner in WINNER_SIDE_TO_TEAM_NUM:
            winner_clan = round_clans.get(tick_int, {}).get(WINNER_SIDE_TO_TEAM_NUM[winner])
        if winner_clan:
            score[winner_clan] = score.get(winner_clan, 0) + 1
        rounds.append({
            "match_id": match_id,
            "round": i + 1,
            "tick": tick_int,
            "winner": winner,
            "winner_clan": winner_clan,
            "reason": row.get("reason"),
            "payload": clean_payload(row),
        })

    teams_map = {}
    for steamid, info in player_info.items():
        teams_map.setdefault(info["team"], []).append({"id": steamid, "name": info["name"]})
    teams = [{"clan_name": clan, "roster": roster} for clan, roster in sorted(teams_map.items())]

    # --- player_round_stats ---------------------------------------------
    # Seed a row per (round, player) from the economy pull first, since it
    # covers every connected player every round regardless of whether they
    # factor into a kill/hurt event that round (an uninvolved player would
    # otherwise get no row at all, even though their money is known).
    stats = {}
    for (round_no, steamid_str), money in economy.items():
        stats[(round_no, steamid_str)] = {
            "match_id": match_id, "round": round_no, "player_id": steamid_str,
            "player_name": player_info.get(steamid_str, {}).get("name"),
            "team": player_info.get(steamid_str, {}).get("team"),
            "kills": 0, "deaths": 0, "assists": 0, "damage_dealt": 0,
            **money,
        }

    def bump(round_no, steamid, name, field, amount=1):
        if round_no is None or steamid is None or pd.isna(steamid):
            return
        steamid_str = str(int(steamid))
        key = (round_no, steamid_str)
        entry = stats.setdefault(key, {
            "match_id": match_id, "round": round_no, "player_id": steamid_str,
            "player_name": name, "team": player_info.get(steamid_str, {}).get("team"),
            "kills": 0, "deaths": 0, "assists": 0, "damage_dealt": 0,
            "balance": None, "equip_value": None, "round_start_equip_value": None, "cash_spent": None,
        })
        if name and not entry["player_name"]:
            entry["player_name"] = name
        entry[field] += amount

    for _, row in kills_df.iterrows():
        tick = row.get("tick")
        round_no = tick_to_round(tick) if tick is not None and not pd.isna(tick) else None
        bump(round_no, row.get("attacker_steamid"), row.get("attacker_name"), "kills")
        bump(round_no, row.get("user_steamid"), row.get("user_name"), "deaths")
        if not pd.isna(row.get("assister_steamid")):
            bump(round_no, row.get("assister_steamid"), row.get("assister_name"), "assists")

    for _, row in hurt_df.iterrows():
        tick = row.get("tick")
        round_no = tick_to_round(tick) if tick is not None and not pd.isna(tick) else None
        dmg = row.get("dmg_health")
        if dmg is not None and not pd.isna(dmg):
            bump(round_no, row.get("attacker_steamid"), row.get("attacker_name"),
                 "damage_dealt", amount=int(dmg))

    return {
        "match_id": match_id,
        "header": header,
        "teams": teams,
        "score": score,
        "raw_events": raw_events,
        "rounds": rounds,
        "player_round_stats": sorted(stats.values(), key=lambda r: (r["round"] or 0, r["player_id"])),
    }


def write_output(decoded: dict, output_dir: Path):
    match_dir = output_dir / decoded["match_id"]
    match_dir.mkdir(parents=True, exist_ok=True)

    with open(match_dir / "header.json", "w", encoding="utf-8") as f:
        json.dump(decoded["header"], f, indent=2)

    with open(match_dir / "match_summary.json", "w", encoding="utf-8") as f:
        json.dump({
            "match_id": decoded["match_id"],
            "map": decoded["header"].get("map_name"),
            "teams": decoded["teams"],
            "score": decoded["score"],
            "rounds_played": len(decoded["rounds"]),
        }, f, indent=2)

    with open(match_dir / "raw_events.jsonl", "w", encoding="utf-8") as f:
        for event in decoded["raw_events"]:
            f.write(json.dumps(event) + "\n")

    with open(match_dir / "rounds.json", "w", encoding="utf-8") as f:
        json.dump(decoded["rounds"], f, indent=2)

    with open(match_dir / "player_round_stats.json", "w", encoding="utf-8") as f:
        json.dump(decoded["player_round_stats"], f, indent=2)

    print(
        f"  -> {len(decoded['raw_events'])} raw_events, {len(decoded['rounds'])} rounds, "
        f"{len(decoded['player_round_stats'])} player_round_stats rows, score {decoded['score']} "
        f"written to {match_dir}"
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("input_path", type=Path, help="A .dem/.rar/.zip file, or a directory to search recursively")
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("./decoded"), help="Where to write per-match JSON output (default: ./decoded)")
    parser.add_argument("--overwrite", action="store_true", help="Re-decode matches that already have output")
    args = parser.parse_args()

    if not args.input_path.exists():
        sys.exit(f"Input path does not exist: {args.input_path}")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    extract_root = args.output_dir / "_extracted"

    demo_files = collect_demo_files(args.input_path, extract_root)
    if not demo_files:
        sys.exit("No .dem files found (directly or inside .rar/.zip archives).")

    print(f"Found {len(demo_files)} demo(s) to decode.")

    decoded_count = 0
    for demo_path in demo_files:
        match_id = match_id_for(demo_path)
        if not args.overwrite and (args.output_dir / match_id / "rounds.json").exists():
            print(f"Skipping {demo_path.name}: already decoded (use --overwrite to redo)")
            continue
        try:
            decoded = decode_demo(demo_path, match_id)
        except Exception as exc:
            print(f"  Failed to decode {demo_path.name}: {exc}")
            continue
        write_output(decoded, args.output_dir)
        decoded_count += 1

    print(f"Done. Decoded {decoded_count}/{len(demo_files)} demo(s) into {args.output_dir}")


if __name__ == "__main__":
    main()
