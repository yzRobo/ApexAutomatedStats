"""Repair per-match RP in Supabase from an RP ledger (rp_ledger.jsonl).

Use when the dashboard shows RP on the wrong games (shifted by one match, blank,
or a missed game's RP landing on the previous match). The ledger is the raw ALS
poll history, so each real RP move is a step between two consecutive readings.
Each match row is matched to the ONE ledger step for that player within
``--window`` seconds of the row's timestamp (ALS usually posts RP 0-35s before
the tracker logs the summary screen), and starting_rp / ending_rp / rp_change
are set from that step. A ranked=False row with a matching step is flipped to
ranked (pubs never move RP). Rows with zero or several candidate steps are left
untouched (ambiguous ones are reported).

IMPORTANT: use the ledger from the PC that actually runs the tracker (stream PC).

    python repair_rp_from_ledger.py path/to/rp_ledger.jsonl            # dry run
    python repair_rp_from_ledger.py path/to/rp_ledger.jsonl --apply    # write

Before applying, the current rows are saved to rp_repair_backup_<time>.json.
Needs NEXT_PUBLIC_SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY in .env (same as
backfill_to_supabase.py).
"""
import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv

EASTERN = timezone(timedelta(hours=-4))


def load_steps(ledger_path):
    """Return ({name: [step, ...]}, first_ts, last_ts) from a ledger file."""
    obs = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obs.append(json.loads(line))
    obs.sort(key=lambda r: r["ts"])
    steps, prev = {}, {}
    for r in obs:
        name, p = r["name"], prev.get(r["name"])
        if p and r.get("rp") is not None and p.get("rp") is not None and r["rp"] != p["rp"]:
            steps.setdefault(name, []).append(
                {"ts": r["ts"], "frm": p["rp"], "to": r["rp"], "d": r["rp"] - p["rp"],
                 "reset": bool(r.get("reset")) or r.get("season") != p.get("season")})
        prev[name] = r
    return steps, obs[0]["ts"], obs[-1]["ts"]


def plan_changes(rows, steps, window, manual_window):
    """Return [(row, {field: new}), ...] and a list of ambiguity messages."""
    changes, notes = [], []
    for x in rows:
        mt = datetime.fromisoformat(x["timestamp"]).timestamp()
        win = manual_window if str(x["session_id"]).startswith("manual-") else window
        cand = [s for s in steps.get(x["name"], []) if abs(s["ts"] - mt) <= win]
        if len(cand) != 1:
            if cand:
                notes.append(f"AMBIGUOUS id={x['id']} {x['name']}: "
                             + ", ".join(f"{s['ts'] - mt:+.0f}s {s['d']:+d}" for s in cand))
            continue
        s = cand[0]
        if s["reset"]:
            notes.append(f"SKIP id={x['id']} {x['name']}: step spans a season reset")
            continue
        new = {"starting_rp": s["frm"], "ending_rp": s["to"], "rp_change": s["d"]}
        if x.get("ranked") is False:
            new["ranked"] = True
        diff = {k: v for k, v in new.items() if x.get(k) != v}
        if diff:
            changes.append((x, diff))
    return changes, notes


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ledger", help="rp_ledger.jsonl from the PC running the tracker")
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry run)")
    ap.add_argument("--since", help="only rows at/after this ISO time (default: ledger start)")
    ap.add_argument("--window", type=float, default=60, help="max |step - match| seconds (default 60)")
    ap.add_argument("--manual-window", type=float, default=150,
                    help="window for hand-entered 'manual-' rows, whose time is approximate (default 150)")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    load_dotenv(os.path.join(here, ".env"))
    url = os.environ["NEXT_PUBLIC_SUPABASE_URL"].rstrip("/") + "/rest/v1/apex_matches"
    key = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    headers = {"apikey": key, "Authorization": "Bearer " + key}

    steps, t0, t1 = load_steps(args.ledger)
    since = args.since or datetime.fromtimestamp(t0 - 3600, timezone.utc).isoformat()
    print(f"ledger {datetime.fromtimestamp(t0, EASTERN):%Y-%m-%d %H:%M} -> "
          f"{datetime.fromtimestamp(t1, EASTERN):%Y-%m-%d %H:%M} ET, players: {', '.join(sorted(steps))}")
    r = requests.get(url, headers=headers, params=[
        ("select", "id,timestamp,session_id,squad_placed,player_slot,name,starting_rp,ending_rp,rp_change,ranked"),
        ("timestamp", "gte." + since), ("order", "timestamp,player_slot")])
    r.raise_for_status()
    rows = r.json()

    changes, notes = plan_changes(rows, steps, args.window, args.manual_window)
    for n in notes:
        print(n)
    for x, d in changes:
        when = datetime.fromisoformat(x["timestamp"]).astimezone(EASTERN)
        print(f"id={x['id']} {when:%m-%d %H:%M} #{x['squad_placed']} {x['name']:<15} "
              f"{x['starting_rp']}->{x['ending_rp']} ({x['rp_change']})  ==>  "
              f"{d.get('starting_rp', x['starting_rp'])}->{d.get('ending_rp', x['ending_rp'])} "
              f"({d.get('rp_change', x['rp_change'])})" + ("  +ranked" if "ranked" in d else ""))
    print(f"{len(changes)} of {len(rows)} rows need changes")

    if not args.apply or not changes:
        if changes:
            print("Dry run - re-run with --apply to write.")
        return
    backup = os.path.join(here, f"rp_repair_backup_{time.strftime('%Y%m%d_%H%M%S')}.json")
    with open(backup, "w", encoding="utf-8") as f:
        json.dump([x for x, _ in changes], f, indent=1)
    print(f"backup of original rows: {backup}")
    ph = dict(headers, **{"Content-Type": "application/json", "Prefer": "return=representation"})
    for x, d in changes:
        p = requests.patch(url, headers=ph, params={"id": f"eq.{x['id']}"}, data=json.dumps(d))
        p.raise_for_status()
        assert len(p.json()) == 1, f"id={x['id']}: expected 1 row updated, got {p.text}"
    print(f"applied {len(changes)} changes")


if __name__ == "__main__":
    main()
