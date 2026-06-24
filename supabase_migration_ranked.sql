-- Migration: add the ranked-match flag to apex_matches.
--
-- WHY: ranked detection (apex_tracker.py ranked_badge_present + the watch-loop
-- latch) reads the top-right HUD rank badge during gameplay and stamps each
-- match as ranked (true) or pub (false). The INSERT in append_match() only
-- includes the `ranked` key once detection has actually run, so syncing keeps
-- working even WITHOUT this column -- but to STORE the flag (and to enable the
-- ranked_detect_enabled feature in config.json) this column must exist, or
-- PostgREST rejects the insert (PGRST204 "column not found").
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL -> New query)
-- BEFORE flipping ranked_detect_enabled to true in config.json.
--
-- Nullable on purpose: rows logged with detection OFF leave it NULL ("unknown"),
-- distinct from false ("detected as a pub"). The dashboard can treat NULL as
-- "not tracked yet". Insert-only anon RLS (supabase_rls.sql) already covers the
-- INSERT path; `ranked` is written at insert time, so no extra GRANT is needed.

ALTER TABLE public.apex_matches
  ADD COLUMN IF NOT EXISTS ranked BOOLEAN;
