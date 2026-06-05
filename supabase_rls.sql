-- Row-Level Security for the shared (friend-distributed) Apex tracker.
--
-- WHY: friends' builds ship the PUBLISHABLE / anon key (safe to distribute).
-- Without RLS, any holder of a key can read/edit/DELETE the whole table. These
-- policies lock the anon role down to INSERT-ONLY: a leaked anon key can append
-- match rows but cannot read, change, or delete anything. The project owner uses
-- the SERVICE_ROLE key, which bypasses RLS entirely (full access for the
-- dashboard / backfill / roster sync).
--
-- Run this once in the Supabase SQL editor (Dashboard -> SQL -> New query).

-- ---------------------------------------------------------------------------
-- apex_matches: friends INSERT match rows; no read/update/delete for anon.
-- ---------------------------------------------------------------------------
ALTER TABLE public.apex_matches ENABLE ROW LEVEL SECURITY;
-- FORCE so even the table owner is subject to policies (service_role still bypasses).
ALTER TABLE public.apex_matches FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anon insert matches" ON public.apex_matches;
CREATE POLICY "anon insert matches"
  ON public.apex_matches
  FOR INSERT
  TO anon
  WITH CHECK (true);

-- No SELECT/UPDATE/DELETE policies for anon = those operations are denied.

-- ---------------------------------------------------------------------------
-- roster: only the owner (service_role) syncs this; anon gets nothing.
-- service_role bypasses RLS, so enabling RLS with no anon policy is enough.
-- ---------------------------------------------------------------------------
ALTER TABLE public.roster ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.roster FORCE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------------
-- OPTIONAL: if you build a public dashboard that reads stats with the anon key,
-- uncomment to also allow read-only access. Leave commented for strict
-- insert-only. (Reads would then be public; writes/deletes still blocked.)
-- ---------------------------------------------------------------------------
-- DROP POLICY IF EXISTS "anon read matches" ON public.apex_matches;
-- CREATE POLICY "anon read matches"
--   ON public.apex_matches FOR SELECT TO anon USING (true);
