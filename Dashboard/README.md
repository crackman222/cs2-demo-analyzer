# Dashboard

Stage 7 of `cs2_match_analytics_architecture.md`: a match-replay dashboard
over everything the earlier stages put in DuckDB. Mostly a pure
presentation layer — nearly every endpoint is a read from
`matches`/`teams`/`players`/`rounds`/`raw_events`/`player_round_stats`/
`match_states`/`team_metrics`/`insights`. The one exception is
`/api/upload` (Upload view), which actually drives the pipeline.

Python (FastAPI) + plain HTML/JS — no Node, no Docker, no build step, no
JS framework. FastAPI queries DuckDB live per request (read-only
connections, safe to run alongside the pipeline scripts).

## Install

```
pip install -r requirements.txt
```

## Usage

```
python app.py [--db PATH] [--port PORT] [--n8n-url URL] [--n8n-team-search-url URL]
```

`--db` defaults to `DuckDBLoader/cs2_matches.duckdb` next to this
script's own repo location — `python app.py` works whether run from the
repo root or from inside `Dashboard/` itself. Then open
`http://127.0.0.1:8000`.

`--n8n-url` (default `http://localhost:5678/webhook/pandascore-discovery`)
and `--n8n-team-search-url` (default
`http://localhost:5678/webhook/pandascore-team-search`) are only used by
the Discover view (see below) — both workflows live in
`n8n-workflows/`, and both need to be **saved and Active** for their
production `/webhook/...` paths to respond; while building/testing in
the n8n editor, only `/webhook-test/...` works, and only while you're
watching the canvas.

Requires the pipeline to have already run for at least one match
(`DuckDBLoader` → `StateReconstructor` → `InsightEngine` →
`EventDetector`) — the dashboard has nothing to show otherwise. The
**Upload** view can do that first run for you (see below).

## Views

The hierarchy is **series → maps → analytics**, matching how a user
thinks about it ("Spirit vs MOUZ" is *the match*; dust2/mirage/etc. are
*maps within it*) even though the underlying `matches` DB table/API is
still one row per map, unchanged — see "Series grouping" below for how
the two meanings coexist without a bigger rename.

- **Library** (`/`) — top-level list, one row per **series**: both teams
  and the maps-won tally (e.g. "MOUZ 3 - 1 Spirit"), tournament, league,
  map count. `GET /api/series`. Click a row to open it.
- **Series** — the maps played within one series: map name, per-map
  score, round count. `GET /api/series/{id}`. Click a map to open its
  analytics (the view below).
- **Discover** — type a team name and pick from an autocomplete dropdown
  (`GET /api/discover/teams`, proxying to n8n's "PandaScore Team Search"
  webhook) — team names collide often enough (e.g. "Spirit" matches both
  "Team Spirit" and "Spirit Academy" as separate PandaScore teams) that
  guessing the first search result isn't good enough; picking explicitly
  resolves a specific team ID. Then set status/date range and search —
  `GET /api/discover`, proxying to n8n's "PandaScore Match Discovery"
  webhook, filtered by that team ID, then cross-referenced against local
  `matches.pandascore_match_id` — a result already decoded shows
  **available** and is clickable straight into its Series view instead of
  the **queue** button (see "Series grouping" below). Results are just
  PandaScore candidates otherwise — nothing here touches HLTV or triggers
  the pipeline; fetching the actual demo is still a manual step, same as
  always. If n8n isn't reachable or a workflow isn't active, this shows
  an error inline rather than the dashboard crashing.
- **Upload** — pick a local `.dem`/`.zip`/`.rar` and run it through the
  full pipeline (`DemoDecoder` → `DuckDBLoader` → `StateReconstructor` →
  `InsightEngine` → `EventDetector`, same scripts as run by hand — see
  "Upload → pipeline" below). Synchronous — the request blocks until
  every stage finishes or one fails, no progress bar; can take a while
  for a large demo (a real 362 MB `.dem` took ~7s end to end when
  re-decoding into a fresh location in testing, but that's this project's
  data — yours may differ). Each stage's log is collapsed by default;
  click a stage to expand it.
- **Match** — score header, a round-by-round strip (orange = round went
  to the map's overall winner, gray = the other clan), both teams'
  scoreboards (K/D/ADR/HS%/rating), a momentum sparkline (the map
  winner's `team_metrics.momentum` per round), and a `flags/` feed of
  every `insights` row for the match in plain-ish language. Click a
  round-strip box to open it.
- **Rounds** — a round list, the selected round's killfeed
  (`raw_events`, attacker/weapon/victim, headshot marked) and economy
  (`match_states` + `team_metrics.equip_value`, with the same
  eco/force/full classification `InsightEngine` uses), and any
  `insights` scoped to that specific round.

## Series grouping & auto-link

`matches` is still one row per **map** — unchanged, since renaming that
throughout the DB/pipeline for zero functional benefit wasn't worth the
risk. What's new is `app.py`'s `series_key()`: `matches.pandascore_match_id`
when set (see below), else the map's own `match_id` — a map that isn't
linked to anything is simply its own series of one, not folded into some
"unknown" bucket. `GET /api/series`/`GET /api/series/{id}` group by this
key; nothing in the DB schema itself represents "a series" as a row —
it's computed at read time from however many maps share a key.

PandaScore match IDs don't correspond to anything on HLTV (no bridge
field exists — finding the actual HLTV page to download is still a
manual, human step, same as always). What the **queue** button *does*
solve: a PandaScore match found via Discover that you intend to go
download gets saved (`POST /api/discover/queue`, straight to DuckDB's
`pending_downloads` table — no n8n involved, this one's a local write).
Later, once you've downloaded and run the decode pipeline,
`DuckDBLoader/load_matches.py` tries to auto-link each newly-loaded match
against queued entries by fuzzy team-name match — if exactly one
`pending_downloads` row's two teams match, that match's
`pandascore_match_id`/`tournament`/`league`/`event_date` get filled in
automatically, which is also what pulls it into a shared series with its
sibling maps. Zero or multiple candidates: left blank, no guessing —
stays its own standalone series until it's linked.

Verified end to end: loaded the 4 existing maps fresh (unlinked — 4
separate series-of-one, correct per-map winners), queued a test entry and
reloaded with `--overwrite` (all 4 correctly merged into one series,
"MOUZ 3 - Spirit 1", tournament/league/date populated), and confirmed
Discover's `available` flag correctly flips true only for the queued
match's own PandaScore id. Reverted the test linkage afterward — the 4
real matches are unlinked again pending an actual Discover search
against real PandaScore data. `GET /api/pending` lists anything queued
that hasn't matched a decoded match yet — no dedicated view for it yet,
just the raw endpoint.

## Upload → pipeline

The upload itself shows real progress (`XMLHttpRequest`'s
`upload.onprogress` — the Fetch API has no upload-progress event, only
download, which is why this one endpoint uses XHR instead of `fetch()`
like everywhere else in this file). There's no progress bar for the
pipeline stages themselves once the file lands on the server, just a
status message — the request stays open until every stage finishes.

`.rar` needs 7-Zip, WinRAR, or unrar installed — Windows has no built-in
RAR support (proprietary format; unlike `.zip`, which Python's stdlib
handles with no extra tool at all). `/api/upload` checks for one
(`patoolib.util.find_program`) before doing anything else — verified
against the real 928 MB `.rar` example filename with no RAR tool present:
rejects in ~2s with a clear message, no file written to `Dashboard/uploads/`,
no pipeline run — rather than writing the whole file, then discovering
the same thing only after decode fails.

`/api/upload` writes the uploaded file to `Dashboard/uploads/` (streamed
to disk in 1 MB chunks, not buffered fully in memory — tested against a
real 362 MB demo), then runs each pipeline stage as a subprocess, same
scripts/args as running them by hand, decoding into `Data/Uploads/`. Each
stage's pass/fail and full stdout+stderr come back in the response so the
frontend can show exactly what happened, not just a spinner.

Each subprocess is invoked with explicit UTF-8 on both ends
(`PYTHONIOENCODING=utf-8` in its environment so it *writes* UTF-8;
`encoding="utf-8", errors="replace"` reading its output back so this end
*reads* it as UTF-8 too) — the pipeline scripts print unicode
(`—`/`→`/`✓`); `subprocess.run`'s default `text=True` decodes using the
console's codepage instead, which on Windows usually isn't UTF-8.
Verified this was a real bug, not a theoretical one: it silently mangled
output, and on a worse byte sequence could raise `UnicodeDecodeError` —
uncaught (only `TimeoutExpired` was handled), so it surfaced as a bare
500 with no `stages` key, crashing the frontend's `for (const s of
data.stages)` with a confusing "data.stages is undefined" rather than
showing what actually happened. `run_stage()` now catches any exception,
not just the timeout, so a stage always comes back stage-shaped; the
frontend also now checks `res.ok` before touching `data.stages` at all,
so a genuine error (e.g. a 409 from the DuckDB write-lock check) shows
its real message instead of the same crash.

If the filename matches HLTV's observed demo-zip naming convention (e.g.
`blast-bounty-2026-season-2-finals-spirit-vs-mouz-bo5--wn1awkp4iFRmswi58ug57`
→ tournament "Blast Bounty 2026 Season 2 Finals", teams Spirit/Mouz,
format bo5), it's queued into `pending_downloads` (`source='hltv_filename'`)
*before* the pipeline runs, so the load stage's existing auto-link picks
it up automatically — same mechanism the Discover queue button feeds,
just a second source into it, so there's only one place
("try_auto_link") that actually decides tournament/league/date.

This is a best-effort heuristic, not a documented HLTV format — it can
fail to match at all (a bare per-map `.dem` filename like
`spirit-vs-mouz-m1-dust2` correctly returns nothing, verified — there's
no series-level info to extract from that shape anyway), and it only
captures one hyphen-token per team, so a multi-word team name (e.g.
`natus-vincere`) only yields its last token (`Vincere`) — verified this
limitation directly rather than assuming it away. When it doesn't match
or under-captures a team name, the upload still runs fine, it just won't
auto-link tournament/league/date for that match.

## Design

Colors/font lifted from the user-supplied "demolab" reference — `#0b0b0c`
background, JetBrains Mono, `#ff9e2c` accent, `#7a7166`/`#a09585` muted
text tiers, `#2b2724`/`#1a1816` hairline borders, CLI-prompt section
headers. Layout is adapted rather than copied: the reference was a
single-player "you vs opponent" tracker; this is a two-team spectator
tool with no "self," so the accent/muted split is applied to "the map's
overall winner" vs "the other clan" instead.

## Notes / gaps

- Round timestamps in the killfeed are a rough `tick / 64` estimate
  (assumes 64-tick), not an exact time — good enough to read the flow of
  a round, not a precise clock.
- No cross-match "Players" career view (the reference's 4th tab) — all 4
  matches here are the same 10 players in one series, so there's limited
  payoff over the per-match scoreboard; worth adding if the dataset grows
  to unrelated matches/players.
- KAST isn't computed (needs trade-kill detection, not yet built).
