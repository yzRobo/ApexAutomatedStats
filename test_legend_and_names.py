"""Unit tests for v1.6.9: legend per game, the middle-card owner fallback, club-tag /
case / rename name matching, and the app's "Last" line picking up the RP verdict.

Runs under pytest, or standalone with the project venv:
``.venv\\Scripts\\python.exe test_legend_and_names.py``. Everything runs on fakes:
the live Supabase client and rank tracker are switched off at import, so nothing
here can reach the real database or ALS.
"""

import csv
import io
import json
import os
import shutil
import tempfile
import time

import apex_tracker as at
import rank_tracker as rtmod

# Never touch the live database or ALS from a test (a harness once inserted a fake
# match into the real table).
at._SUPABASE_CLIENT = None
at._RANK_TRACKER = None

ROSTER = ["yzRobo", "UltVaultHunter", "NameJFK", "Rotarynerd", "TCremer22", "SaWaii",
          "HeckaTrix"]
CFG = {"known_names": ROSTER, "name_match_cutoff": 0.8}


# --------------------------------------------------------------------------- #
# rank tracker: presence readings
# --------------------------------------------------------------------------- #
def _new_tracker(ledger=None):
    """A real RankTracker with an empty poll list: its thread starts but never calls
    ALS. Readings are injected through _fetch_one with a faked urlopen."""
    rt = rtmod.RankTracker(api_key="test", known_names=[], ledger_path=ledger)
    rt.stop()
    return rt


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _bridge(name, rp, legend, state="inMatch", since=None, uid="1"):
    return {"global": {"name": name, "uid": uid,
                       "rank": {"rankScore": rp, "rankName": "Gold", "rankDiv": 2}},
            "realtime": {"isInGame": 1, "selectedLegend": legend, "currentState": state,
                         "currentStateSinceTimestamp": since}}


def _fetch(rt, roster_name, payload):
    real = rtmod.urllib.request.urlopen
    rtmod.urllib.request.urlopen = lambda req, timeout=10: _Resp(json.dumps(payload).encode())
    try:
        rt._fetch_one(roster_name)
    finally:
        rtmod.urllib.request.urlopen = real


def _put(rt, name, ts, state, since, legend):
    with rt._lock:
        rt._presence.setdefault(name, rtmod.deque(maxlen=400)).append(
            {"ts": ts, "state": state, "since": since, "legend": legend})


def test_fetch_records_presence_name_and_ledger():
    tmp = tempfile.mkdtemp()
    try:
        ledger = os.path.join(tmp, "rp_ledger.jsonl")
        rt = _new_tracker(ledger)
        since = time.time() - 300
        _fetch(rt, "yzRobo", _bridge("yzRobo", 5011, "Wraith", since=since))
        # a degraded rankScore (0) still records presence
        _fetch(rt, "NameJFK", _bridge("NameJFK", 0, "Lifeline", since=since + 4))
        assert rt._presence["yzRobo"][-1]["legend"] == "Wraith"
        assert abs(rt._presence["yzRobo"][-1]["since"] - since) < 1e-6
        assert rt._presence["NameJFK"][-1]["legend"] == "Lifeline"
        assert rt._als_names == {"yzRobo": "yzRobo", "NameJFK": "NameJFK"}
        lines = [json.loads(l) for l in open(ledger, encoding="utf-8")]
        assert len(lines) == 1 and lines[0]["legend"] == "Wraith" and lines[0]["since"] == since
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_presence_survives_restart_via_ledger():
    tmp = tempfile.mkdtemp()
    try:
        ledger = os.path.join(tmp, "rp_ledger.jsonl")
        rt = _new_tracker(ledger)
        since = time.time() - 600
        _fetch(rt, "yzRobo", _bridge("yzRobo", 5011, "Wraith", since=since))
        rt2 = _new_tracker(ledger)  # a restart mid-match
        assert rt2.match_legends(["yzRobo"]) == {"yzRobo": "Wraith"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_match_legends_uses_latest_in_match_reading():
    rt = _new_tracker()
    now = time.time()
    s = now - 900
    _put(rt, "yzRobo", now - 880, "inMatch", s, "Bangalore")  # legend select, old pick
    _put(rt, "yzRobo", now - 300, "inMatch", s, "Wraith")
    _put(rt, "yzRobo", now - 20, "inLobby", now - 25, "Wraith")  # left after the summary
    _put(rt, "NameJFK", now - 290, "inMatch", s + 4, "Lifeline")
    assert rt.match_legends(["yzRobo", "NameJFK", "random"], at=now) == \
        {"yzRobo": "Wraith", "NameJFK": "Lifeline"}


def test_match_legends_drops_readings_from_other_matches():
    rt = _new_tracker()
    now = time.time()
    s = now - 700
    _put(rt, "yzRobo", now - 60, "inMatch", s, "Wraith")
    _put(rt, "UltVaultHunter", now - 50, "inMatch", s + 3, "Rampart")
    # no reading this match: their last in-match reading is from the game before
    _put(rt, "NameJFK", now - 900, "inMatch", now - 2000, "Lifeline")
    # quit early and is already in another game
    _put(rt, "TCremer22", now - 30, "inMatch", now - 90, "Pathfinder")
    got = rt.match_legends(["yzRobo", "UltVaultHunter", "NameJFK", "TCremer22"], at=now)
    assert got == {"yzRobo": "Wraith", "UltVaultHunter": "Rampart"}, got


def test_match_legends_two_players_one_already_in_next_game():
    # yzRobo + SaWaii + a random: SaWaii died early and queued into another game.
    rt = _new_tracker()
    now = time.time()
    _put(rt, "yzRobo", now - 40, "inMatch", now - 900, "Wraith")
    _put(rt, "SaWaii", now - 30, "inMatch", now - 120, "Octane")
    names = ["yzRobo", "SaWaii"]
    # this PC's player is on the summary, so their match is the one that ended
    assert rt.match_legends(names, at=now, anchor="yzRobo") == {"yzRobo": "Wraith"}
    # no anchor and a 1-1 split: nothing is guessed
    assert rt.match_legends(names, at=now) == {}


def test_match_legends_short_game_without_a_poll_gets_nothing():
    # M1 logged 6 min ago; M2 was a hot drop no poll landed in. The latest in-match
    # readings are M1's and must not be used for M2.
    rt = _new_tracker()
    now = time.time()
    m1_start, m1_logged = now - 1500, now - 360
    for n, leg in (("yzRobo", "Wraith"), ("SaWaii", "Loba"), ("NameJFK", "Gibby")):
        _put(rt, n, now - 400, "inMatch", m1_start, leg)
        _put(rt, n, now - 300, "inLobby", m1_logged, leg)
    names = ["yzRobo", "SaWaii", "NameJFK"]
    assert rt.match_legends(names, at=now, anchor="yzRobo", after=m1_logged) == {}
    # whereas a reading from M2 itself is kept
    _put(rt, "yzRobo", now - 60, "inMatch", now - 240, "Octane")
    assert rt.match_legends(names, at=now, anchor="yzRobo", after=m1_logged) == {"yzRobo": "Octane"}


def test_match_legends_window_and_single_player():
    rt = _new_tracker()
    now = time.time()
    _put(rt, "yzRobo", now - 3 * 3600, "inMatch", now - 3 * 3600 - 300, "Wraith")
    assert rt.match_legends(["yzRobo"], at=now) == {}  # hours old: not this match
    _put(rt, "yzRobo", now - 40, "inMatch", now - 500, "Octane")
    assert rt.match_legends(["yzRobo"], at=now) == {"yzRobo": "Octane"}
    _put(rt, "SaWaii", now - 40, "inMatch", None, "Loba")  # no since from ALS: accepted
    assert rt.match_legends(["yzRobo", "SaWaii"], at=now) == {"yzRobo": "Octane", "SaWaii": "Loba"}


def test_in_match_recently():
    rt = _new_tracker()
    now = time.time()
    assert rt.in_match_recently("yzRobo", at=now) is None  # no data
    _put(rt, "yzRobo", now - 200, "offline", None, "Wraith")
    assert rt.in_match_recently("yzRobo", at=now) is False
    _put(rt, "yzRobo", now - 100, "inMatch", now - 600, "Wraith")
    assert rt.in_match_recently("yzRobo", at=now) is True


def test_name_aliases_follow_a_rename():
    rt = _new_tracker()
    _fetch(rt, "SaWaii", _bridge("SaWaiiTTV", 7492, "Loba"))
    _fetch(rt, "yzRobo", _bridge("YZROBO", 5151, "Wraith"))  # case only: not a rename
    assert rt.name_aliases() == {"SaWaiiTTV": "SaWaii"}


# --------------------------------------------------------------------------- #
# name matching
# --------------------------------------------------------------------------- #
def test_snap_name_case_and_club_tags():
    for read, want in [("YZROBO", "yzRobo"), ("[CPAP] yzRobo", "yzRobo"),
                       ("[TTV] SaWaii", "SaWaii"), ("(TTV) SaWaii", "SaWaii"),
                       ("[TTV]SaWaii", "SaWaii"), ("|TTV] SaWaii", "SaWaii"),
                       ("{ABC} heckatrix", "HeckaTrix"), ("ullVaultHunter", "UltVaultHunter")]:
        assert at.snap_name(read, CFG) == want, (read, at.snap_name(read, CFG))


def test_snap_name_leaves_others_alone():
    # a different account with a CJK prefix, randoms with tags, and short garbage
    for read in ["澤井-SaWai", "[DIH] ImStillBaci", "[VODS] ZyStruct", "FK", "s", "a", "Mxariol",
                 # near-misses that only reach the cutoff when case is ignored
                 "NameJack", "Hacktrix", "Swaii", "Sawa", "[TTV] Sawa"]:
        assert at.snap_name(read, CFG) == read, (read, at.snap_name(read, CFG))


def test_snap_name_aliases():
    cfg = dict(CFG, name_aliases={"CamTheMan": "TCremer22", "Ghost": "NotOnRoster"})
    assert at.snap_name("CamTheMan", cfg) == "TCremer22"
    assert at.snap_name("[XO] CamTheMan", cfg) == "TCremer22"
    assert at.snap_name("Ghost", cfg) == "Ghost"  # alias to a non-roster name is ignored
    for bad in (["CamTheMan"], "CamTheMan", 5):  # a hand-edited config mistake
        assert at.snap_name("yzRobo", dict(CFG, name_aliases=bad)) == "yzRobo"
    real = at._NAME_ALIASES
    try:
        at._NAME_ALIASES = {"SaWaiiTTV": "SaWaii"}
        assert at.snap_name("SaWaiiTTV", CFG) == "SaWaii"
    finally:
        at._NAME_ALIASES = real


# --------------------------------------------------------------------------- #
# middle-card owner fallback
# --------------------------------------------------------------------------- #
def _write_csv(path, mids):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=at.CSV_FIELDS)
        w.writeheader()
        for i, mid in enumerate(mids):
            for slot, name in ((1, "NameJFK"), (2, mid), (3, "TCremer22")):
                w.writerow({"timestamp": f"2026-11-0{1 + i % 7}T10:{i:02d}:00-05:00",
                            "session_id": f"4:1:2:{i}", "player_slot": slot, "name": name})


def test_resolve_owner_from_history_and_config():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "m.csv")
        _write_csv(p, ["yzRobo"] * 12 + ["FK"])
        assert at.resolve_owner(CFG, p) == "yzRobo"
        _write_csv(p, ["yzRobo"] * 5)  # too few matches to be sure
        assert at.resolve_owner(CFG, p) is None
        _write_csv(p, ["yzRobo", "SaWaii"] * 8)  # two people play on this PC
        assert at.resolve_owner(CFG, p) is None
        assert at.resolve_owner(dict(CFG, owner_name="yzrobo"), p) == "yzRobo"
        assert at.resolve_owner(dict(CFG, owner_name="Nobody"), p) is None
        assert at.resolve_owner(CFG, os.path.join(tmp, "missing.csv")) is None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _match(mid="FK", left="NameJFK", right="TCremer22"):
    return {"session_id": "2:1021623:11662379:0141cc99", "squad_placed": 8,
            "total_squad_kills": 0,
            "players": [{"player_slot": i + 1, "name": n, "kills": 0, "assists": 0,
                         "knocks": k, "damage": d, "revive_given": 0, "respawn_given": 0}
                        for i, (n, d, k) in enumerate([(left, 35, 0), (mid, 314, 1),
                                                        (right, 408, 0)])]}


class _FakeTracker:
    def __init__(self, legends=None, owner_in_match=True):
        self.legends, self.owner_in_match = legends or {}, owner_in_match

    def get_rp(self, name):
        return {"NameJFK": (9206, None), "TCremer22": (11651, None),
                "yzRobo": (12436, None)}.get(name, (None, None))

    def match_legends(self, names, at=None, anchor=None, after=None):
        self.called = {"anchor": anchor, "after": after}
        return {n: l for n, l in self.legends.items() if n in names}

    def in_match_recently(self, name, at=None):
        return self.owner_in_match

    def health(self):
        return "ok", None


def test_owner_fallback_rules():
    m = _match("FK")
    assert at._apply_owner_fallback(m, CFG, "yzRobo") == "FK"
    assert m["players"][1]["name"] == "yzRobo"
    m = _match("")  # blank middle name: rescued too
    assert at._apply_owner_fallback(m, CFG, "yzRobo") == "" and m["players"][1]["name"] == "yzRobo"
    for m, owner in [(_match("SaWaii"), "yzRobo"),           # a roster name: trusted
                     (_match("FK", left="yzRobo"), "yzRobo"),  # owner already shown
                     (_match("FK"), None),                     # owner unknown
                     (_match("FK"), "NotRostered")]:
        before = m["players"][1]["name"]
        assert at._apply_owner_fallback(m, CFG, owner) is None
        assert m["players"][1]["name"] == before
    real = at._RANK_TRACKER
    try:
        at._RANK_TRACKER = _FakeTracker(owner_in_match=False)  # owner offline: someone else
        m = _match("AltAccount")
        assert at._apply_owner_fallback(m, CFG, "yzRobo") is None
        assert m["players"][1]["name"] == "AltAccount"
        at._RANK_TRACKER = _FakeTracker(owner_in_match=None)  # no presence data: trust it
        assert at._apply_owner_fallback(_match("FK"), CFG, "yzRobo") == "FK"
    finally:
        at._RANK_TRACKER = real


# --------------------------------------------------------------------------- #
# logging a match: owner fallback + legends into the CSV and Supabase
# --------------------------------------------------------------------------- #
class _InsertDB:
    """Fake Supabase client that records inserts and can reject named columns."""

    def __init__(self, missing=()):
        self.inserts, self.missing = [], set(missing)

    def table(self, name):
        db = self

        class _T:
            def insert(self, rows, returning=None):
                class _X:
                    def execute(self_inner):
                        for c in db.missing:
                            if any(c in r for r in rows):
                                raise Exception(f"{{'code': 'PGRST204', 'message': "
                                                f"\"Could not find the '{c}' column\"}}")
                        db.inserts.append([dict(r) for r in rows])
                return _X()
        return _T()


def _log_match(match, tracker, owner="yzRobo", db=None):
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex_matches.csv")
    status = {"logged_this_run": 0, "owner": owner, "last_match": None}
    real = (at.extract_match, at._RANK_TRACKER, at._SUPABASE_CLIENT, at._enqueue_rp_pending)
    queued = []
    at.extract_match = lambda frame, cfg, scale: json.loads(json.dumps(match))
    at._RANK_TRACKER, at._SUPABASE_CLIENT = tracker, db
    at._enqueue_rp_pending = lambda m, p: queued.append(m)
    try:
        stable = {"fp": None, "ft": 0.0}
        r1 = at._try_log_match(None, CFG, None, path, set(), status, lambda e: None, None,
                               stable=stable, frame_ts=1.0)
        r2 = at._try_log_match(None, CFG, None, path, set(), status, lambda e: None, None,
                               stable=stable, frame_ts=2.0)
        rows = at._read_csv_rows(path)
        return (r1, r2), rows, status, queued
    finally:
        at.extract_match, at._RANK_TRACKER, at._SUPABASE_CLIENT, at._enqueue_rp_pending = real
        shutil.rmtree(tmp, ignore_errors=True)


def test_logged_match_gets_owner_and_legends():
    db = _InsertDB()
    tracker = _FakeTracker({"yzRobo": "Wraith", "NameJFK": "Lifeline"})
    res, rows, status, queued = _log_match(_match("FK"), tracker, db=db)
    assert res == ("incomplete", "logged"), res
    assert [r["name"] for r in rows] == ["NameJFK", "yzRobo", "TCremer22"]
    assert [r["legend"] for r in rows] == ["Lifeline", "Wraith", ""]
    assert [r.get("legend") for r in db.inserts[0]] == ["Lifeline", "Wraith", None]
    assert [p["legend"] for p in status["last_match"]["players"]] == ["Lifeline", "Wraith", None]
    assert queued and queued[0]["players"][1]["legend"] == "Wraith"
    assert tracker.called["anchor"] == "yzRobo"  # this PC's player anchors the match
    assert status["last_logged_epoch"] and abs(status["last_logged_epoch"] - time.time()) < 60


def test_no_legend_key_when_none_known():
    db = _InsertDB()
    res, rows, _, _ = _log_match(_match("yzRobo"), _FakeTracker({}), db=db)
    assert res[1] == "logged"
    assert all("legend" not in r for r in db.inserts[0])
    assert all(r["legend"] == "" for r in rows)


def test_insert_without_migrated_columns_still_syncs():
    for missing in (["legend"], ["legend", "ranked"]):
        db = _InsertDB(missing=missing)
        real = at._SUPABASE_CLIENT
        at._SUPABASE_CLIENT = db
        try:
            rows = [{"name": "yzRobo", "damage": 1, "legend": "Wraith", "ranked": True}]
            assert at._supabase_insert(rows) is True
        finally:
            at._SUPABASE_CLIENT = real
        assert all(not (set(missing) & set(r)) for r in db.inserts[0]), db.inserts
    db = _InsertDB()

    class _Down:
        def table(self, name):
            raise Exception("connection refused")
    real = at._SUPABASE_CLIENT
    at._SUPABASE_CLIENT = _Down()
    try:
        assert at._supabase_insert([{"name": "x", "legend": "Wraith"}]) is False
    finally:
        at._SUPABASE_CLIENT = real


def test_old_csv_gains_legend_column():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "apex_matches.csv")
        old = [c for c in at.CSV_FIELDS if c != "legend"]
        with open(p, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=old)
            w.writeheader()
            w.writerow({"timestamp": "t", "session_id": "s", "name": "yzRobo", "ranked": "True"})
        m = _match("yzRobo")
        for q in m["players"]:
            q.update(starting_rp=None, ending_rp=None, rp_change=None, legend=None)
        m["players"][1]["legend"] = "Wraith"
        csv_ok, db_ok = at.append_match(p, m)
        assert csv_ok and db_ok is None
        rows = at._read_csv_rows(p)
        assert list(rows[0].keys()) == at.CSV_FIELDS
        assert rows[0]["ranked"] == "True" and rows[0]["legend"] == ""
        assert rows[2]["legend"] == "Wraith"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# "Last" line picks up the resolver's verdict; legend reaches the surviving copy
# --------------------------------------------------------------------------- #
def test_last_line_takes_resolved_ranked_and_rp():
    sid = "4:1022419:11739316:02eebfl"
    status = {"last_match": {"session_id": sid, "ranked": False, "rp_note": "no RP baseline",
                             "players": [{"name": "yzRobo", "rp_change": None},
                                         {"name": "NameJFK", "rp_change": None}]}}
    at._refresh_last_match(status)
    assert status["last_match"]["ranked"] is False  # nothing resolved yet
    at._note_resolved(sid, "yzRobo", {"ending_rp": 5151, "rp_change": 140, "ranked": True})
    at._refresh_last_match(status)
    lm = status["last_match"]
    assert lm["ranked"] is True and lm["rp_note"] is None
    assert [p["rp_change"] for p in lm["players"]] == [140, None]
    for i in range(30):  # bounded
        at._note_resolved(f"s{i}", "x", {"rp_change": 1})
    assert len(at._RESOLVED_MATCHES) <= 20


def test_legend_copied_to_surviving_copy():
    from test_scanner_reliability import _FakeDB, _session, _hint, _with_db
    t0 = time.time()
    db = _FakeDB(_session("other:sid", t0 - 20))
    for r in db.rows:
        r["legend"] = None
    hint = _hint(t0)
    hint["yzRobo"]["legend"] = "Wraith"
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine:sid", {"yzRobo": {"ending_rp": 12264, "rp_change": -46}}, hint))
    assert db.rows[0]["rp_change"] == -46 and db.rows[0]["legend"] == "Wraith"
    assert all(r["legend"] is None for r in db.rows[1:])


if __name__ == "__main__":
    import sys
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL  {t.__name__}  {e!r}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {t.__name__}  {e!r}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed.")
    sys.exit(1 if failed else 0)
