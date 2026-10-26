-- Cross-PC dedup trigger, v2. Run once in the Supabase SQL editor (replaces the function only; the
-- existing trg_apex_matches_dedup trigger keeps pointing at it). Safe to run more than once.
--
-- Same job as supabase_dedup.sql: when two squadmates' trackers log the same game, keep the first copy and
-- remove the later one. Three fixes, found in the v1.6.8 scanner review:
--  * A time window (+/-90 min). v1 compared against EVERY past match, so a new low-damage game whose
--    placement/kills/damage happened to equal any old game would have been deleted.
--  * Low-information matches are never treated as copies (fewer than 2 named players, or no damage at all),
--    mirroring the dashboard's matchFingerprint.
--  * A NULL total_squad_kills counts as sum(kills). Trackers before v1.6.8 store NULL on 1-kill games and
--    v1.6.8+ stores 1, so without this a mixed pair would no longer be recognised as one game.
-- RP note: the deleted copy may be the one whose PC tracks RP. v1.6.8 trackers on the service key detect
-- that (their RP patch matches 0 rows) and write the RP onto the surviving copy instead.

create or replace function public.apex_matches_dedup_stmt()
returns trigger language plpgsql security definer set search_path = public as $$
declare
  new_sid text;
  new_ts  timestamptz;
  named   int;
  dmg     bigint;
  new_fp  text;
  dup     boolean;
begin
  -- This insert is one match: its rows share a session_id.
  select max(session_id), min(timestamp),
         count(*) filter (where coalesce(name, '') <> ''),
         coalesce(sum(damage), 0)
    into new_sid, new_ts, named, dmg
  from inserted;

  if named < 2 or dmg = 0 then
    return null;  -- too little to tell two games apart: keep it
  end if;

  select coalesce(max(squad_placed)::text, '') || '|' ||
         coalesce(max(total_squad_kills), sum(coalesce(kills, 0)))::text || '|' ||
         string_agg(coalesce(damage, 0)::text || ':' || coalesce(kills, 0)::text,
                    ',' order by coalesce(damage, 0), coalesce(kills, 0))
    into new_fp
  from inserted;

  select exists (
    select 1 from public.apex_matches m
    where m.session_id <> new_sid
      and m.timestamp between new_ts - interval '90 minutes' and new_ts + interval '90 minutes'
    group by m.session_id
    having coalesce(max(m.squad_placed)::text, '') || '|' ||
           coalesce(max(m.total_squad_kills), sum(coalesce(m.kills, 0)))::text || '|' ||
           string_agg(coalesce(m.damage, 0)::text || ':' || coalesce(m.kills, 0)::text,
                      ',' order by coalesce(m.damage, 0), coalesce(m.kills, 0)) = new_fp
  ) into dup;

  if dup then
    delete from public.apex_matches where session_id = new_sid;
  end if;

  return null;
end;
$$;
