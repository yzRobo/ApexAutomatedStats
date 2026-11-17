-- Migration: add the map played to apex_matches (tracker v1.7.0+).
--
-- WHY: Apex Legends Status has no per-match map, but its map-rotation endpoint gives
-- each playlist's current and next map with start/end times. From v1.7.0 the tracker
-- keeps every rotation window it sees and stamps each match with the maps that were
-- up when the match BEGAN (the players' in-match since time from ALS presence):
--   pubs_map   - the pubs (battle royale) map
--   ranked_map - the ranked map
-- Both are stored because the ranked flag can still change after insert (the RP
-- resolver may prove a "pub" was ranked); the dashboard shows ranked_map for a ranked
-- match and pubs_map otherwise. Older games, and games logged without an ALS key or
-- without a covering rotation window, stay NULL.
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL -> New query). Until it
-- runs, inserts still work: the tracker drops the columns from the insert and keeps
-- the maps in its CSV only (it logs a note saying so). Idempotent.
--
-- No GRANT needed: the anon insert policy (supabase_rls.sql) is table-wide, and the
-- maps are written at insert time. The owner's service key copies them to the
-- surviving row when the cross-PC dedup trigger removed this PC's copy.
ALTER TABLE public.apex_matches
  ADD COLUMN IF NOT EXISTS pubs_map TEXT,
  ADD COLUMN IF NOT EXISTS ranked_map TEXT;
