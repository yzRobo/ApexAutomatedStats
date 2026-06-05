-- Create the apex_matches table to mirror the CSV structure
CREATE TABLE public.apex_matches (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    session_id TEXT NOT NULL,
    squad_placed INTEGER,
    total_squad_kills INTEGER,
    player_slot INTEGER NOT NULL,
    name TEXT,
    kills INTEGER,
    assists INTEGER,
    knocks INTEGER,
    damage INTEGER,
    revive_given INTEGER,
    respawn_given INTEGER
);

-- Optional: Add an index on session_id since we will frequently query by it
CREATE INDEX idx_apex_matches_session_id ON public.apex_matches (session_id);
-- Optional: Add an index on name for player leaderboards
CREATE INDEX idx_apex_matches_name ON public.apex_matches (name);
