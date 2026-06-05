-- Roster table: the single source of truth for "who is a regular".
-- The tracker upserts config.json -> known_names here on startup
-- (see sync_roster_to_supabase in apex_tracker.py), and the Next.js
-- dashboard reads it via fetchRoster().
--
-- Run this once in the Supabase SQL editor.

CREATE TABLE IF NOT EXISTS public.roster (
    name TEXT PRIMARY KEY,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The dashboard reads with the public (publishable/anon) key. The apex_matches
-- table is already readable that way; keep roster consistent. If you have RLS
-- enabled project-wide, add a read policy so the dashboard can SELECT:
--
-- ALTER TABLE public.roster ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "roster public read" ON public.roster FOR SELECT USING (true);
--
-- (The tracker writes with the service role key, which bypasses RLS.)
