-- Migration: add ALS rank/RP tracking columns to apex_matches.
--
-- WHY: the rank tracker (rank_tracker.py + apex_tracker.py) now records each
-- player's RP at match time (starting_rp), the post-EA-cache value (ending_rp),
-- and the delta (rp_change). The INSERT in append_match() spreads every player
-- field into the row, so WITHOUT these columns PostgREST rejects the whole
-- insert (PGRST204 "column not found") and ALL match syncing silently stops.
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL -> New query)
-- BEFORE running a build that includes the rank tracker.

-- ---------------------------------------------------------------------------
-- 1. Columns (idempotent).
-- ---------------------------------------------------------------------------
ALTER TABLE public.apex_matches
  ADD COLUMN IF NOT EXISTS starting_rp INTEGER,
  ADD COLUMN IF NOT EXISTS ending_rp   INTEGER,
  ADD COLUMN IF NOT EXISTS rp_change   INTEGER;

-- ---------------------------------------------------------------------------
-- 2. Scoped UPDATE for the anon role (distributed friend builds).
--
-- The RP "ending" value lands 2-3 min after a match (EA cache delay), so the
-- tracker INSERTs the row first, then UPDATEs ending_rp/rp_change once resolved.
-- supabase_rls.sql keeps anon strictly INSERT-only, which would deny that
-- UPDATE for friends. We re-open UPDATE for anon but limit the blast radius to
-- just the two RP columns via a column-level GRANT: a leaked anon key can
-- amend ending_rp/rp_change on existing rows, but CANNOT touch kills, damage,
-- placement, names, or delete anything. The owner's service_role bypasses RLS.
-- ---------------------------------------------------------------------------
GRANT UPDATE (ending_rp, rp_change) ON public.apex_matches TO anon;

DROP POLICY IF EXISTS "anon update rp" ON public.apex_matches;
CREATE POLICY "anon update rp"
  ON public.apex_matches
  FOR UPDATE
  TO anon
  USING (true)
  WITH CHECK (true);
