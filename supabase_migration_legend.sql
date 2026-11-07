-- Migration: add the legend played to apex_matches (tracker v1.6.9+).
--
-- WHY: the tracker already polls Apex Legends Status for each roster player's RP; the
-- same response carries realtime.selectedLegend, which during a match is the legend
-- being played. From v1.6.9 the tracker stores it on each roster player's row. Rows
-- for randoms, older games, and games with no in-match ALS reading stay NULL.
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL -> New query). Until it
-- runs, inserts still work: the tracker drops the column from the insert and keeps the
-- legend in its CSV only (it logs a note saying so).
--
-- No GRANT needed: the anon insert policy (supabase_rls.sql) is table-wide, and the
-- legend is written at insert time. The owner's service key copies it to the
-- surviving row when the cross-PC dedup trigger removed this PC's copy.
ALTER TABLE public.apex_matches
  ADD COLUMN IF NOT EXISTS legend TEXT;
