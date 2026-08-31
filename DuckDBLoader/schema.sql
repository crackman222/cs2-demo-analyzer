-- DuckDB schema for cs2-match-tracker.
-- Mirrors Section 16 of cs2_match_analytics_architecture.md.
--
-- Tables populated by DuckDBLoader/load_matches.py, from
-- DemoDecoder/decode_demos.py's output:
--   matches, teams, players, raw_events, rounds, player_round_stats
--
-- Populated by StateReconstructor/reconstruct_state.py (Stage 4):
--   match_states
--
-- Tables created empty here and reserved for later stages (not written by
-- either of the above):
--   team_metrics, player_metrics  (Stage 5 — initial insights)
--   insights       (Stages 5-6 — insight engine, event detection)
--   predictions    (Stage 9 — prediction layer)

CREATE TABLE IF NOT EXISTS matches (
    match_id VARCHAR PRIMARY KEY,
    map VARCHAR,
    rounds_played INTEGER,
    loaded_at TIMESTAMP DEFAULT now()
);

CREATE TABLE IF NOT EXISTS teams (
    match_id VARCHAR REFERENCES matches(match_id),
    clan_name VARCHAR,
    score INTEGER,
    PRIMARY KEY (match_id, clan_name)
);

CREATE TABLE IF NOT EXISTS players (
    match_id VARCHAR REFERENCES matches(match_id),
    player_id VARCHAR,
    name VARCHAR,
    clan_name VARCHAR,
    PRIMARY KEY (match_id, player_id)
);

-- player/target kept as JSON (not flattened into attacker_id/name/team
-- columns) to mirror Section 3's normalized event shape directly.
CREATE TABLE IF NOT EXISTS raw_events (
    event_id VARCHAR PRIMARY KEY,
    match_id VARCHAR REFERENCES matches(match_id),
    tick BIGINT,
    round INTEGER,
    event_type VARCHAR,
    player JSON,
    target JSON,
    weapon VARCHAR,
    payload JSON
);

CREATE TABLE IF NOT EXISTS rounds (
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    tick BIGINT,
    winner VARCHAR,
    winner_clan VARCHAR,
    reason VARCHAR,
    payload JSON,
    PRIMARY KEY (match_id, round)
);

CREATE TABLE IF NOT EXISTS player_round_stats (
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    player_id VARCHAR,
    player_name VARCHAR,
    team VARCHAR,
    kills INTEGER,
    deaths INTEGER,
    assists INTEGER,
    damage_dealt INTEGER,
    balance INTEGER,
    equip_value INTEGER,
    round_start_equip_value INTEGER,
    cash_spent INTEGER,
    PRIMARY KEY (match_id, round, player_id)
);

-- Populated by StateReconstructor/reconstruct_state.py (Stage 4). One row
-- per (match, round, clan) rather than the wide team_a/team_b shape
-- Section 5 originally sketched — normalized against `teams.clan_name`
-- instead of an arbitrary "which side is team_a" labeling.
--
-- score: cumulative rounds won through this round, inclusive.
-- money: this clan's summed player balance at this round's freeze_end
--        (i.e. money entering this round, same sample point as
--        player_round_stats.balance).
-- alive: this clan's survivors at this round's conclusion (an outcome
--        stat, not a mid-round live value — a single per-round row can't
--        capture alive count changing within the round).
-- bomb_state: reserved, NULL until DemoDecoder parses bomb events
--             (not implemented yet).
CREATE TABLE IF NOT EXISTS match_states (
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    clan_name VARCHAR,
    score INTEGER,
    money INTEGER,
    alive INTEGER,
    bomb_state VARCHAR,
    reconstructed_at TIMESTAMP DEFAULT now(),
    PRIMARY KEY (match_id, round, clan_name)
);

-- --- Reserved for later stages -------------------------------------------

CREATE TABLE IF NOT EXISTS team_metrics (
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    clan_name VARCHAR,
    metric VARCHAR,
    value DOUBLE
);

CREATE TABLE IF NOT EXISTS player_metrics (
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    player_id VARCHAR,
    metric VARCHAR,
    value DOUBLE
);

CREATE TABLE IF NOT EXISTS insights (
    id VARCHAR PRIMARY KEY,
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    insight_type VARCHAR,
    clan_name VARCHAR,
    confidence DOUBLE,
    data JSON
);

CREATE TABLE IF NOT EXISTS predictions (
    match_id VARCHAR REFERENCES matches(match_id),
    round INTEGER,
    team_a_probability DOUBLE,
    team_b_probability DOUBLE,
    model_version VARCHAR,
    input_features JSON,
    actual_winner VARCHAR
);
