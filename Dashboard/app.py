"""
app.py

Stage 7 of cs2_match_analytics_architecture.md: a match-replay dashboard
over what every earlier stage put in DuckDB. Mostly a pure presentation
layer — most endpoints are just reads against matches/teams/players/
rounds/raw_events/player_round_stats/match_states/team_metrics/insights.
The one exception is /api/upload, which actually drives the pipeline
(DemoDecoder -> DuckDBLoader -> StateReconstructor -> InsightEngine ->
EventDetector) via subprocess, same scripts as run manually today.

FastAPI queries DuckDB live, per request (read-only connection — safe to
run alongside the pipeline scripts, since DuckDB allows concurrent
readers; it's only a second read-write connection that would conflict).

Usage:
    python app.py [--db PATH] [--port PORT]

Requires: fastapi, uvicorn, duckdb, requests, python-multipart
    (pip install -r requirements.txt)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import duckdb
import requests
import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

STATIC_DIR = Path(__file__).parent / "static"
UPLOAD_DIR = Path(__file__).parent / "uploads"
REPO_ROOT = Path(__file__).parent.parent
DECODE_OUTPUT_DIR = REPO_ROOT / "Data" / "Uploads"
DB_DEFAULT = REPO_ROOT / "DuckDBLoader" / "cs2_matches.duckdb"
N8N_URL_DEFAULT = "http://localhost:5678/webhook/pandascore-discovery"
N8N_TEAM_SEARCH_URL_DEFAULT = "http://localhost:5678/webhook/pandascore-team-search"

# Best-effort match against HLTV's observed demo-zip naming convention,
# e.g. "blast-bounty-2026-season-2-finals-spirit-vs-mouz-bo5--wn1awkp4iFRmswi58ug57"
# -> tournament="Blast Bounty 2026 Season 2 Finals", team_a="Spirit",
# team_b="Mouz", format="bo5". This is NOT a documented HLTV format, just
# an observed pattern — it can fail to match (returns None) rather than
# guess, and only captures a single hyphen-token per team, so a
# multi-word team name (e.g. "natus-vincere") only yields its last token.
HLTV_ZIP_PATTERN = re.compile(
    r"^(?P<tournament>.+?)-(?P<team_a>[a-z0-9]+)-vs-(?P<team_b>[a-z0-9]+)-bo(?P<bo>\d)(?:--.+)?$",
    re.IGNORECASE,
)

app = FastAPI(title="cs2-match-tracker dashboard")
db_path: Path = None  # set in main()
n8n_url: str = None  # set in main()
n8n_team_search_url: str = None  # set in main()


def query(sql: str, params: list = None) -> list[dict]:
    """Run a read-only query, return rows as a list of dicts."""
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        cur = con.execute(sql, params or [])
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def execute_write(sql: str, params: list = None):
    """Run a write statement. Unlike query(), this needs a read-write
    connection — DuckDB allows only one of those at a time, so this can
    fail if the DuckDB CLI or a pipeline script is holding it open (the
    same conflict already seen with the loader scripts)."""
    try:
        con = duckdb.connect(str(db_path))
    except duckdb.IOException as exc:
        raise HTTPException(
            409, f"Database is locked by another connection (DuckDB CLI or a pipeline "
                 f"script open?) — close it and retry: {exc}",
        )
    try:
        con.execute(sql, params or [])
    finally:
        con.close()


def require_match(match_id: str):
    if not query("SELECT 1 FROM matches WHERE match_id = ?", [match_id]):
        raise HTTPException(404, f"No such match: {match_id}")


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def series_key(m: dict) -> str:
    """A series (BO1/BO3/BO5) groups several per-map `matches` rows —
    grouped by pandascore_match_id when the auto-link (Discover queue or
    an uploaded HLTV zip's filename) set one. A map with no link is its
    own series of one, keyed by its own match_id — not folded into some
    "unknown" bucket, since that's not a real grouping, just an absence
    of one."""
    return m["pandascore_match_id"] or m["match_id"]


@app.get("/api/series")
def list_series():
    """Top-level dashboard view: one row per series (what a user calls
    "a match" — Spirit vs MOUZ, not "map 2 of Spirit vs MOUZ"), grouping
    the per-map `matches` rows via series_key(). Games-won tally comes
    from each map's higher-score team, not round score."""
    matches = query("""
        SELECT match_id, map, pandascore_match_id, tournament, league, event_date
        FROM matches
    """)
    grouped: dict[str, dict] = {}
    for m in matches:
        sid = series_key(m)
        grouped.setdefault(sid, {
            "series_id": sid, "tournament": m["tournament"], "league": m["league"],
            "event_date": m["event_date"], "match_ids": [],
        })["match_ids"].append(m["match_id"])

    result = []
    for sid, s in grouped.items():
        games_won: dict[str, int] = {}
        for match_id in s["match_ids"]:
            map_teams = query(
                "SELECT clan_name, score FROM teams WHERE match_id = ? ORDER BY score DESC", [match_id],
            )
            if map_teams:
                winner = map_teams[0]["clan_name"]
                games_won[winner] = games_won.get(winner, 0) + 1
            for t in map_teams:
                # Ensure a team that lost every map still shows up with 0
                # wins, rather than being absent from the tally entirely.
                games_won.setdefault(t["clan_name"], 0)
        teams = sorted(
            ({"clan_name": name, "maps_won": wins} for name, wins in games_won.items()),
            key=lambda t: -t["maps_won"],
        )
        result.append({
            "series_id": sid, "tournament": s["tournament"], "league": s["league"],
            "event_date": s["event_date"], "maps_played": len(s["match_ids"]), "teams": teams,
        })

    result.sort(key=lambda s: s["event_date"] or "", reverse=True)
    return result


@app.get("/api/series/{series_id}")
def series_detail(series_id: str):
    """Maps-in-a-series view: every map (per-map `matches` row) belonging
    to this series, reached by clicking a series on the top-level view."""
    matches = query("""
        SELECT match_id, map, rounds_played, tournament, league, event_date
        FROM matches
        WHERE pandascore_match_id = ? OR match_id = ?
        ORDER BY match_id
    """, [series_id, series_id])
    if not matches:
        raise HTTPException(404, f"No such series: {series_id}")
    for m in matches:
        m["teams"] = query(
            "SELECT clan_name, score FROM teams WHERE match_id = ? ORDER BY score DESC",
            [m["match_id"]],
        )
    return {"series_id": series_id, "matches": matches}


@app.get("/api/matches/{match_id}")
def match_overview(match_id: str):
    """Match view: score header, round strip, scoreboards, momentum
    timeline, and flags (insights)."""
    require_match(match_id)

    header = query("SELECT match_id, map, rounds_played FROM matches WHERE match_id = ?", [match_id])[0]
    teams = query("SELECT clan_name, score FROM teams WHERE match_id = ? ORDER BY score DESC", [match_id])

    rounds = query(
        "SELECT round, winner_clan, reason FROM rounds WHERE match_id = ? ORDER BY round", [match_id],
    )

    # Scoreboard: player_metrics (whole-match aggregates) + headshot% from
    # raw_events (not stored as its own metric, cheap to derive here).
    scoreboard = query("""
        SELECT p.player_id, p.name, p.clan_name,
               MAX(CASE WHEN pm.metric='kills' THEN pm.value END) AS kills,
               MAX(CASE WHEN pm.metric='deaths' THEN pm.value END) AS deaths,
               MAX(CASE WHEN pm.metric='assists' THEN pm.value END) AS assists,
               MAX(CASE WHEN pm.metric='adr' THEN pm.value END) AS adr,
               MAX(CASE WHEN pm.metric='rating' THEN pm.value END) AS rating
        FROM players p
        JOIN player_metrics pm ON pm.match_id = p.match_id AND pm.player_id = p.player_id
        WHERE p.match_id = ?
        GROUP BY p.player_id, p.name, p.clan_name
    """, [match_id])
    hs_counts = query("""
        SELECT json_extract_string(player, '$.id') AS player_id,
               SUM(CASE WHEN json_extract(payload, '$.headshot') = true THEN 1 ELSE 0 END) AS headshots,
               COUNT(*) AS kills
        FROM raw_events WHERE match_id = ? AND event_type = 'kill'
        GROUP BY json_extract_string(player, '$.id')
    """, [match_id])
    hs_by_player = {r["player_id"]: (r["headshots"] / r["kills"] * 100 if r["kills"] else 0) for r in hs_counts}
    for row in scoreboard:
        row["hs_pct"] = round(hs_by_player.get(row["player_id"], 0), 1)

    momentum = query("""
        SELECT round, clan_name, value FROM team_metrics
        WHERE match_id = ? AND metric = 'momentum' ORDER BY round
    """, [match_id])

    flags = query("""
        SELECT round, insight_type, clan_name, confidence, data
        FROM insights WHERE match_id = ? ORDER BY round
    """, [match_id])
    for f in flags:
        f["data"] = json.loads(f["data"])

    return {
        "match": header, "teams": teams, "rounds": rounds,
        "scoreboard": scoreboard, "momentum": momentum, "flags": flags,
    }


@app.get("/api/matches/{match_id}/rounds/{round_}")
def round_detail(match_id: str, round_: int):
    """Rounds view: killfeed, economy, and any flags for one round."""
    require_match(match_id)

    round_row = query(
        "SELECT round, winner_clan, reason FROM rounds WHERE match_id = ? AND round = ?",
        [match_id, round_],
    )
    if not round_row:
        raise HTTPException(404, f"No such round: {round_}")

    killfeed = query("""
        SELECT tick, player, target, weapon,
               CASE WHEN json_extract(payload, '$.headshot') = true THEN true ELSE false END AS headshot
        FROM raw_events
        WHERE match_id = ? AND round = ? AND event_type = 'kill'
        ORDER BY tick
    """, [match_id, round_])
    for k in killfeed:
        k["player"] = json.loads(k["player"]) if k["player"] else None
        k["target"] = json.loads(k["target"]) if k["target"] else None

    economy = query("""
        SELECT clan_name, score, money, alive FROM match_states
        WHERE match_id = ? AND round = ? ORDER BY clan_name
    """, [match_id, round_])
    buy = query("""
        SELECT clan_name, value AS equip_value FROM team_metrics
        WHERE match_id = ? AND round = ? AND metric = 'equip_value' ORDER BY clan_name
    """, [match_id, round_])
    buy_by_clan = {b["clan_name"]: b["equip_value"] for b in buy}
    for e in economy:
        equip = buy_by_clan.get(e["clan_name"], 0)
        e["equip_value"] = equip
        e["buy_type"] = "eco" if equip < 10_000 else ("force" if equip < 20_000 else "full")

    flags = query("""
        SELECT insight_type, clan_name, confidence, data FROM insights
        WHERE match_id = ? AND round = ? ORDER BY insight_type
    """, [match_id, round_])
    for f in flags:
        f["data"] = json.loads(f["data"])

    return {"round": round_row[0], "killfeed": killfeed, "economy": economy, "flags": flags}


@app.get("/api/discover/teams")
def discover_teams(q: str = ""):
    """Proxy to the n8n 'PandaScore Team Search' webhook — candidate teams
    for the Discover form's autocomplete, so the user picks the exact
    team (e.g. "Team Spirit" vs "Spirit Academy") instead of the backend
    silently guessing the first search result."""
    if not q:
        return []
    try:
        resp = requests.get(n8n_team_search_url, params={"q": q}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            502, f"Couldn't reach the n8n team-search workflow at {n8n_team_search_url}: {exc}",
        )
    return resp.json()


@app.get("/api/discover")
def discover(team_id: str = "", status: str = "finished", date_from: str = "", date_to: str = ""):
    """Proxy to the n8n 'PandaScore Match Discovery' webhook. n8n does the
    actual PandaScore call (filter[opponent_id] against /csgo/matches);
    this forwards the filters, then cross-references each result against
    matches.pandascore_match_id so the frontend can mark results already
    decoded locally as clickable straight into the Series view — the only
    non-passthrough thing this endpoint does, and still no HLTV
    automation or pipeline triggering, matching the workflow's scope.
    team_id must already be resolved (via /api/discover/teams) — this
    endpoint doesn't do name matching itself."""
    if not team_id:
        raise HTTPException(400, "team_id is required — pick a team from the autocomplete first.")
    params = {"team_id": team_id, "status": status, "date_from": date_from, "date_to": date_to}
    try:
        resp = requests.get(n8n_url, params=params, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            502, f"Couldn't reach the n8n discovery workflow at {n8n_url}: {exc}",
        )
    results = resp.json()

    linked_ids = {
        r["pandascore_match_id"]
        for r in query("SELECT DISTINCT pandascore_match_id FROM matches WHERE pandascore_match_id IS NOT NULL")
    }
    for m in results:
        m["available"] = str(m.get("match_id")) in linked_ids
    return results


class QueueRequest(BaseModel):
    pandascore_match_id: str
    team_a: str = ""
    team_b: str = ""
    date: str = ""
    tournament: str = ""
    league: str = ""


@app.post("/api/discover/queue")
def queue_download(req: QueueRequest):
    """A Discover result the user intends to go download — saved so
    DuckDBLoader/load_matches.py can later auto-fill tournament/league/
    event_date onto the decoded match(es), instead of that data getting
    lost or needing manual re-entry. Re-queueing the same match is a
    no-op, not an error."""
    execute_write("""
        INSERT OR REPLACE INTO pending_downloads
            (pandascore_match_id, team_a, team_b, event_date, tournament, league)
        VALUES (?, ?, ?, ?, ?, ?)
    """, [req.pandascore_match_id, req.team_a, req.team_b, req.date, req.tournament, req.league])
    return {"queued": True}


@app.get("/api/pending")
def list_pending():
    """Queued matches not yet linked to any decoded match — visibility/
    debugging only, no dedicated frontend view for this yet."""
    return query("""
        SELECT * FROM pending_downloads
        WHERE pandascore_match_id NOT IN (
            SELECT DISTINCT pandascore_match_id FROM matches WHERE pandascore_match_id IS NOT NULL
        )
        ORDER BY queued_at DESC
    """)


def parse_hltv_filename(stem: str) -> dict | None:
    """See HLTV_ZIP_PATTERN's comment. Returns None (not a guess) if the
    filename doesn't match the observed shape — e.g. a bare per-map .dem
    filename like "spirit-vs-mouz-m1-dust2" won't match (no "boN" token),
    which is correct: that shape has no series-level tournament info to
    extract in the first place."""
    m = HLTV_ZIP_PATTERN.match(stem)
    if not m:
        return None
    return {
        "tournament": m.group("tournament").replace("-", " ").replace("_", " ").strip().title(),
        "team_a": m.group("team_a").title(),
        "team_b": m.group("team_b").title(),
        "format": f"bo{m.group('bo')}",
    }


def run_stage(*args: str, timeout: int = 1800) -> dict:
    """Run one pipeline stage script as a subprocess — same scripts, same
    way they're run by hand today (python <script> <args...>), just
    invoked from here instead of a terminal. Captures stdout/stderr so
    the frontend can show what actually happened, not just pass/fail.

    Explicit UTF-8 on both ends, not subprocess.run's default text=True:
    the pipeline scripts print unicode (—, →, ✓); text=True decodes child
    output using the console's codepage, which on Windows usually isn't
    UTF-8 — verified this actually mangles output (an em dash came back
    as "\\u00e2\\u20ac\\u201d") and can outright raise UnicodeDecodeError
    on a worse byte sequence, which — since only TimeoutExpired was
    caught here — would propagate as an unhandled 500 with no `stages`
    key, exactly the "data.stages is undefined" bug this was chasing.
    PYTHONIOENCODING forces the child to *write* UTF-8; encoding+errors
    forces this end to *read* it as UTF-8, replacing any genuinely bad
    byte instead of throwing. Both sides catch any exception now, not
    just the timeout, so a stage always comes back stage-shaped."""
    script = args[0]
    cmd = [sys.executable, str(REPO_ROOT / script), *args[1:]]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    try:
        result = subprocess.run(
            cmd, capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
        return {
            "stage": script, "ok": result.returncode == 0,
            "output": (result.stdout or "") + (result.stderr or ""),
        }
    except subprocess.TimeoutExpired:
        return {"stage": script, "ok": False, "output": f"Timed out after {timeout}s"}
    except Exception as exc:
        return {"stage": script, "ok": False, "output": f"{type(exc).__name__}: {exc}"}


# Same candidate list patoolib itself reports when extraction fails —
# checked here too so a missing tool fails in milliseconds, before
# writing a potentially huge file to disk, rather than only discovering
# it after DemoDecoder tries and fails to extract.
RAR_TOOL_CANDIDATES = ["unrar", "7z", "7zz", "7zzs", "rar", "unar"]


def rar_extraction_available() -> bool:
    try:
        import patoolib
    except ImportError:
        return False
    return any(patoolib.util.find_program(p) for p in RAR_TOOL_CANDIDATES)


@app.post("/api/upload")
async def upload_demo(file: UploadFile):
    """Upload a .dem/.rar/.zip and run it through the full pipeline —
    DemoDecoder -> DuckDBLoader -> StateReconstructor -> InsightEngine ->
    EventDetector — the same chain run manually today, just triggered
    from here. Synchronous: this can take a while for a large demo: the
    request blocks until every stage finishes (or one fails), no
    background job/progress-polling — deliberately simple for a
    single-user local tool.

    If the filename matches HLTV's observed zip-naming convention, the
    parsed team names/tournament get queued into pending_downloads first
    (source='hltv_filename') so the load step's existing auto-link picks
    them up — the same mechanism Discover's "queue" button feeds, just a
    second source into it."""
    if file.filename.lower().endswith(".rar") and not rar_extraction_available():
        # FastAPI has already fully received the upload by the time this
        # handler runs (UploadFile parsing happens before the endpoint
        # body) — this doesn't save upload time, but it does fail in
        # milliseconds rather than after minutes of pointless pipeline
        # stages that were never going to find anything to decode.
        raise HTTPException(
            422,
            "No RAR-capable tool found (checked for: " + ", ".join(RAR_TOOL_CANDIDATES) + "). "
            "Windows has no built-in RAR support (proprietary format, unlike .zip which works "
            "with no extra tool) — install 7-Zip (free, https://www.7-zip.org/) and try again.",
        )

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / file.filename
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            f.write(chunk)

    parsed = parse_hltv_filename(dest.stem)
    if parsed:
        execute_write("""
            INSERT OR REPLACE INTO pending_downloads
                (pandascore_match_id, team_a, team_b, tournament, source)
            VALUES (?, ?, ?, ?, 'hltv_filename')
        """, [f"hltv:{dest.stem}", parsed["team_a"], parsed["team_b"], parsed["tournament"]])

    stages = [run_stage("DemoDecoder/decode_demos.py", str(dest), "-o", str(DECODE_OUTPUT_DIR))]
    if stages[-1]["ok"]:
        stages.append(run_stage("DuckDBLoader/load_matches.py", str(DECODE_OUTPUT_DIR), "--db", str(db_path)))
        stages.append(run_stage("StateReconstructor/reconstruct_state.py", "--db", str(db_path)))
        stages.append(run_stage("InsightEngine/compute_insights.py", "--db", str(db_path)))
        stages.append(run_stage("EventDetector/detect_events.py", "--db", str(db_path)))

    return {"success": all(s["ok"] for s in stages), "parsed_filename": parsed, "stages": stages}


# ---------------------------------------------------------------------------
# Static frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


def main():
    global db_path, n8n_url, n8n_team_search_url
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--db", type=Path, default=DB_DEFAULT, help=f"DuckDB database file (default: {DB_DEFAULT})")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--n8n-url", default=N8N_URL_DEFAULT, help=f"n8n discovery webhook URL (default: {N8N_URL_DEFAULT})")
    parser.add_argument("--n8n-team-search-url", default=N8N_TEAM_SEARCH_URL_DEFAULT, help=f"n8n team-search webhook URL (default: {N8N_TEAM_SEARCH_URL_DEFAULT})")
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}")
    db_path = args.db.resolve()
    n8n_url = args.n8n_url
    n8n_team_search_url = args.n8n_team_search_url

    print(f"Serving {db_path} at http://127.0.0.1:{args.port}")
    print(f"Discover view calls n8n at {n8n_url} and {n8n_team_search_url}")
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
