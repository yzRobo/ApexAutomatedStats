-- Migration: add a LIVE current-rank snapshot to the roster table.
--
-- WHY: the ALS rank tracker (rank_tracker.py) already polls each known player's
-- current RP + rank tier + division every rank_poll_seconds, with or without a
-- game running. That snapshot is exactly what the dashboard's rank badge needs,
-- so we persist it per player here instead of waiting for a ranked match to be
-- played and resolved. The owner's tracker upserts these columns each poll cycle
-- (see _sync_ranks_to_supabase in apex_tracker.py); the dashboard reads them in
-- fetchPlayerRanks().
--
-- Safe alongside the existing roster sync: sync_roster_to_supabase upserts only
-- {name} on_conflict=name, so it updates just `name` and never clobbers these.
--
-- Style note: nullable columns, no constraints — a schema that can reject an
-- upsert would silently drop the snapshot. The dashboard validates on read.
--
-- Idempotent: safe to run more than once.

ALTER TABLE public.roster
  ADD COLUMN IF NOT EXISTS current_rp      INTEGER,
  ADD COLUMN IF NOT EXISTS rank_tier       TEXT,      -- ALS rankName, e.g. 'Platinum' / 'Apex Predator'
  ADD COLUMN IF NOT EXISTS rank_division   SMALLINT,  -- ALS rankDiv: 1=I (highest) .. 4=IV (lowest); NULL/0 for Master/Predator
  ADD COLUMN IF NOT EXISTS rank_updated_at TIMESTAMPTZ;
