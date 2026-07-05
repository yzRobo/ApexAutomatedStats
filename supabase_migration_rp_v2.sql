-- Migration: RP v2 — let the resolver UPDATE the `ranked` flag after insert.
--
-- WHY: in RP v2 (RP_TRACKING_V2_SPEC.md §4.2) the RP resolver classifies each
-- match at RESOLVE time, not at log time. A match is inserted first (ranked may
-- be NULL/unknown), and later — once the ALS reading propagates — the resolver
-- patches it: an observed RP change PROVES the match was ranked, so it flips
-- `ranked` to true alongside ending_rp/rp_change; a zero-confirmed pub sets
-- `ranked` false. So `ranked` now arrives/flips asynchronously exactly like the
-- RP columns already do.
--
-- The owner build writes with the SERVICE_ROLE key (bypasses RLS) and is
-- unaffected. FRIEND builds write with the insert-only anon key, whose UPDATE was
-- scoped by supabase_migration_rp.sql to ONLY (ending_rp, rp_change) via a
-- column-level GRANT. Without extending that grant, the anon resolver's attempt
-- to patch `ranked` is rejected (the Python code retries WITHOUT ranked so RP
-- still lands, but the flag won't store). This migration widens the scoped grant
-- to include `ranked` so friend builds can persist the resolved flag too.
--
-- Blast radius is unchanged in spirit: a leaked anon key can still only amend
-- ending_rp / rp_change / ranked on existing rows — never kills, damage,
-- placement, names, and it cannot INSERT arbitrary edits or DELETE. Run this once
-- in the Supabase SQL editor (Dashboard -> SQL -> New query) BEFORE distributing
-- a v2 friend build. Idempotent — safe to run more than once.

-- ---------------------------------------------------------------------------
-- 1. Ensure the `ranked` column exists (no-op if migration_ranked already ran).
-- ---------------------------------------------------------------------------
ALTER TABLE public.apex_matches
  ADD COLUMN IF NOT EXISTS ranked BOOLEAN;

-- ---------------------------------------------------------------------------
-- 2. Extend the scoped anon UPDATE grant to include `ranked`.
--    (Column-level GRANTs are additive; naming all three keeps the intent clear
--    and the statement idempotent.)
-- ---------------------------------------------------------------------------
GRANT UPDATE (ending_rp, rp_change, ranked) ON public.apex_matches TO anon;

-- ---------------------------------------------------------------------------
-- 3. Re-affirm the row-level UPDATE policy (unchanged from migration_rp.sql).
--    The policy governs WHICH rows; the column GRANT above governs WHICH columns.
-- ---------------------------------------------------------------------------
DROP POLICY IF EXISTS "anon update rp" ON public.apex_matches;
CREATE POLICY "anon update rp"
  ON public.apex_matches
  FOR UPDATE
  TO anon
  USING (true)
  WITH CHECK (true);
