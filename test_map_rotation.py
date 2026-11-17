"""Unit tests for v1.7.0: the map played, from the ALS map rotation.

Runs under pytest, or standalone: ``python test_map_rotation.py``. Everything runs on
fakes: the live Supabase client and rank tracker are switched off at import, so
nothing here can reach the real database or ALS.
"""

import json
import os
import shutil
import tempfile
import time

import apex_tracker as at
import rank_tracker as rtmod

# Never touch the live database or ALS from a test.
at._SUPABASE_CLIENT = None
at._RANK_TRACKER = None

from test_legend_and_names import (_FakeTracker, _InsertDB, _Resp, _log_match,  # noqa: E402
                                   _match, _new_tracker, _put)
from test_scanner_reliability import _FakeDB, _hint, _session, _with_db  # noqa: E402

H = 3600


def _rotation(now, br=("Storm Point", "E-District"), ranked=("E-District", "Broken Moon"),
              br_start=None, ranked_start=None):
    """A maprotation (version=2) payload: 90 min pubs windows, 270 min ranked ones."""
    bs = now - 30 * 60 if br_start is None else br_start
    rs = now - 2 * H if ranked_start is None else ranked_start
    return {
        "battle_royale": {
            "current": {"start": bs, "end": bs + 5400, "map": br[0], "code": "x"},
            "next": {"start": bs + 5400, "end": bs + 10800, "map": br[1]}},
        "ranked": {
            "current": {"start": rs, "end": rs + 16200, "map": ranked[0]},
            "next": {"start": rs + 16200, "end": rs + 32400, "map": ranked[1]}},
        "ltm": {"current": {"start": now, "end": now + 900, "map": "Skulltown"}},
    }


def _serve(payload, fn):
    real = rtmod.urllib.request.urlopen
    rtmod.urllib.request.urlopen = lambda req, timeout=10: _Resp(json.dumps(payload).encode())
    try:
        fn()
    finally:
        rtmod.urllib.request.urlopen = real


def _tracker_with_rotation(payload, path=None):
    rt = _new_tracker()
    rt._rotation_path = path
    _serve(payload, rt._fetch_rotation)
    return rt


# --------------------------------------------------------------------------- #
# rank tracker: rotation windows
# --------------------------------------------------------------------------- #
def test_fetch_stores_windows_and_map_at():
    now = int(time.time())
    rt = _tracker_with_rotation(_rotation(now))
    assert rt.map_at("battle_royale", now) == "Storm Point"
    assert rt.map_at("battle_royale", now + 2 * H) == "E-District"
    assert rt.map_at("ranked", now) == "E-District"
    assert rt.map_at("ranked", now + 4 * H) == "Broken Moon"
    assert rt.map_at("battle_royale", now - 2 * H) is None  # before anything fetched
    assert "ltm" not in rt._rotation


def test_windows_kept_after_rotation_moves_on_and_survive_restart():
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "map_rotation.json")
        now = int(time.time())
        first = now - 2 * H - 1800  # Storm Point from 2.5 h ago, then E-District
        rt = _tracker_with_rotation(_rotation(now, br_start=first), path)
        # Later ALS only reports the next pair; the earlier windows are kept.
        later = _rotation(now, br=("E-District", "Broken Moon"), br_start=first + 5400)
        _serve(later, rt._fetch_rotation)
        assert rt.map_at("battle_royale", now - 2 * H) == "Storm Point"
        assert rt.map_at("battle_royale", now) == "E-District"
        rt2 = _new_tracker()
        rt2._rotation_path = path
        rt2._load_rotation()
        assert rt2.map_at("battle_royale", now - 2 * H) == "Storm Point"
        assert rt2.map_at("battle_royale", now) == "E-District"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_a_new_fetch_replaces_overlapping_windows():
    now = int(time.time())
    rt = _tracker_with_rotation(_rotation(now))
    # ALS reshuffles the schedule: the same span now has different maps and bounds.
    _serve(_rotation(now, br=("Olympus", "Kings Canyon"), br_start=now - 10 * 60),
           rt._fetch_rotation)
    assert rt.map_at("battle_royale", now) == "Olympus"
    assert rt.map_at("battle_royale", now - 20 * 60) is None  # old window dropped


def test_failed_or_empty_fetch_keeps_windows_and_backs_off():
    now = int(time.time())
    rt = _tracker_with_rotation(_rotation(now))
    rt._rotation_path = os.devnull  # turns the schedule on
    real = rtmod.urllib.request.urlopen

    def boom(req, timeout=10):
        raise OSError("ALS down")
    rtmod.urllib.request.urlopen = boom
    try:
        rt._rotation_fetched = 0.0
        rt._fetch_rotation()
    finally:
        rtmod.urllib.request.urlopen = real
    assert rt.map_at("battle_royale", now) == "Storm Point"
    assert not rt._rotation_due(time.time())  # waits before retrying
    rt2 = _tracker_with_rotation({"battle_royale": {"current": {"map": "x"}}})
    assert rt2.map_at("battle_royale", now) is None and rt2._rotation_fetched == 0.0


def test_rotation_due_rules():
    now = int(time.time())
    rt = _tracker_with_rotation(_rotation(now))
    assert not rt._rotation_due(now)          # no path: never fetches
    rt._rotation_path = os.devnull
    rt._rotation_fetched = now
    rt._rotation_retry_at = 0.0               # look past the gap after a fetch
    assert not rt._rotation_due(now)          # fresh and covered
    assert rt._rotation_due(now + 31 * 60)    # 30 min refresh
    rt._rotation_fetched = now + 4 * H
    assert rt._rotation_due(now + 2 * H + 1500)  # pubs windows run out within 10 min


def test_uncovered_mode_does_not_refetch_every_cycle():
    now = int(time.time())
    no_ranked = _rotation(now)
    del no_ranked["ranked"]  # e.g. between ranked splits
    rt = _tracker_with_rotation(no_ranked)
    rt._rotation_path = os.devnull
    t = time.time()
    assert rt.map_at("ranked", t) is None
    assert not rt._rotation_due(t + 30)            # next poll cycle: no new request
    assert rt._rotation_due(t + rtmod._ROTATION_RETRY + 1)


def test_extract_windows_tolerates_junk():
    assert rtmod._extract_windows({}, "ranked") == []
    assert rtmod._extract_windows({"ranked": None}, "ranked") == []
    assert rtmod._extract_windows({"ranked": {"current": {"start": 5, "end": 1, "map": "x"}}},
                                  "ranked") == []
    assert rtmod._extract_windows({"ranked": {"current": {"start": 1, "end": 5, "map": " "}}},
                                  "ranked") == []
    assert rtmod._extract_windows([], "ranked") == []


# --------------------------------------------------------------------------- #
# rank tracker: map for a match
# --------------------------------------------------------------------------- #
def test_match_straddling_a_rotation_gets_the_map_it_began_on():
    now = int(time.time())
    flip = now - 5 * 60  # pubs went Storm Point -> E-District 5 min ago
    rt = _tracker_with_rotation(_rotation(now, br_start=flip - 5400))
    assert rt.map_at("battle_royale", now) == "E-District"
    s = now - 15 * 60  # the match began on Storm Point
    _put(rt, "yzRobo", now - 120, "inMatch", s, "Wraith")
    _put(rt, "NameJFK", now - 110, "inMatch", s + 3, "Lifeline")
    maps = rt.match_maps(["yzRobo", "NameJFK"], at=now, anchor="yzRobo")
    assert maps == {"battle_royale": "Storm Point", "ranked": "E-District"}, maps


def test_match_without_a_start_time():
    now = int(time.time())
    rt = _tracker_with_rotation(_rotation(now, br_start=now - 20 * 60))
    # No match start time: the pubs map flipped 20 min ago, inside the 30 min the
    # match could have begun in, so pubs is unknown; ranked has been E-District for
    # 2 h, so it is known.
    _put(rt, "yzRobo", now - 60, "inLobby", now - 70, "Wraith")
    assert rt.match_maps(["yzRobo"], at=now) == {"battle_royale": None, "ranked": "E-District"}
    # ...unless the previous match was logged after the flip.
    assert rt.match_maps(["yzRobo"], at=now, after=now - 15 * 60) == \
        {"battle_royale": "Storm Point", "ranked": "E-District"}
    # A window that covers the whole 30 min is known without help.
    rt2 = _tracker_with_rotation(_rotation(now, br_start=now - 30 * 60))
    assert rt2.match_maps(["yzRobo"], at=now)["battle_royale"] == "Storm Point"


def test_match_start_uncovered_gives_none():
    now = int(time.time())
    rt = _tracker_with_rotation(_rotation(now))
    _put(rt, "yzRobo", now - 60, "inMatch", now - 40 * 60, "Wraith")  # before the pubs window
    assert rt.match_maps(["yzRobo"], at=now, anchor="yzRobo") == \
        {"battle_royale": None, "ranked": "E-District"}


def test_match_start_matches_legend_clustering():
    rt = _new_tracker()
    now = time.time()
    s = now - 700
    _put(rt, "yzRobo", now - 60, "inMatch", s, None)        # no legend still counts
    _put(rt, "NameJFK", now - 50, "inMatch", s + 2, "Lifeline")
    assert rt.match_start(["yzRobo", "NameJFK"], at=now) in (s, s + 2)
    assert rt.match_start(["yzRobo", "NameJFK"], at=now, anchor="NameJFK") == s + 2
    assert rt.match_start(["nobody"], at=now) is None
    # legends are unchanged by the refactor: the legend-less reading is skipped
    assert rt.match_legends(["yzRobo", "NameJFK"], at=now) == {"NameJFK": "Lifeline"}


# --------------------------------------------------------------------------- #
# logging a match: maps into the CSV, Supabase, and the app's Last line
# --------------------------------------------------------------------------- #
class _MapTracker(_FakeTracker):
    def __init__(self, maps, **kw):
        super().__init__(**kw)
        self.maps = maps

    def match_maps(self, names, at=None, anchor=None, after=None):
        self.map_called = {"names": list(names), "anchor": anchor, "after": after}
        return dict(self.maps)


def test_logged_match_gets_both_maps():
    db = _InsertDB()
    tracker = _MapTracker({"battle_royale": "Storm Point", "ranked": "E-District"})
    res, rows, status, queued = _log_match(_match("yzRobo"), tracker, db=db)
    assert res[1] == "logged", res
    assert all(r["pubs_map"] == "Storm Point" and r["ranked_map"] == "E-District" for r in rows)
    assert all(r["pubs_map"] == "Storm Point" and r["ranked_map"] == "E-District"
               for r in db.inserts[0])
    assert tracker.map_called["anchor"] == "yzRobo"
    assert "yzRobo" in tracker.map_called["names"]
    lm = status["last_match"]
    # The test match has no ranked flag and the two maps differ, so none is shown yet.
    assert lm["map"] is None and lm["maps"] == {"pubs": "Storm Point", "ranked": "E-District"}


def test_no_map_keys_when_unknown():
    db = _InsertDB()
    for maps in ({}, {"battle_royale": None, "ranked": None}):
        res, rows, _, _ = _log_match(_match("yzRobo"), _MapTracker(maps), db=db)
        assert res[1] == "logged"
        assert all("pubs_map" not in r and "ranked_map" not in r for r in db.inserts[-1])
        assert all(r["pubs_map"] == "" for r in rows)


def test_one_known_map_still_stores_both_keys():
    db = _InsertDB()
    _log_match(_match("yzRobo"), _MapTracker({"battle_royale": None, "ranked": "E-District"}),
               db=db)
    assert all(r["pubs_map"] is None and r["ranked_map"] == "E-District" for r in db.inserts[0])


def test_insert_without_map_migration_still_syncs():
    db = _InsertDB(missing=["pubs_map", "ranked_map"])
    real = at._SUPABASE_CLIENT
    at._SUPABASE_CLIENT = db
    try:
        rows = [{"name": "yzRobo", "damage": 1, "legend": "Wraith", "ranked": True,
                 "pubs_map": "Storm Point", "ranked_map": "E-District"}]
        assert at._supabase_insert(rows) is True
    finally:
        at._SUPABASE_CLIENT = real
    ins = db.inserts[0][0]
    assert "pubs_map" not in ins and "ranked_map" not in ins
    assert ins["ranked"] is True and ins["legend"] == "Wraith"  # not stripped by mistake


def test_shown_map_follows_ranked_flag():
    assert at._shown_map(True, "Storm Point", "E-District") == "E-District"
    assert at._shown_map(False, "Storm Point", "E-District") == "Storm Point"
    assert at._shown_map(None, "Storm Point", "E-District") is None
    assert at._shown_map(None, "Olympus", "Olympus") == "Olympus"


def test_last_line_map_follows_resolved_ranked():
    status = {"last_match": {"session_id": "s1", "ranked": False, "map": "Storm Point",
                             "maps": {"pubs": "Storm Point", "ranked": "E-District"},
                             "players": [{"name": "yzRobo", "rp_change": None}]}}
    at._note_resolved("s1", "yzRobo", {"ranked": True, "rp_change": 41})
    at._refresh_last_match(status)
    assert status["last_match"]["ranked"] is True
    assert status["last_match"]["map"] == "E-District"


def test_maps_copied_to_surviving_copy():
    t0 = time.time()
    db = _FakeDB(_session("other:sid", t0 - 20))
    for r in db.rows:
        r["pubs_map"] = r["ranked_map"] = None
    hint = _hint(t0)
    hint["yzRobo"].update(pubs_map="Storm Point", ranked_map="E-District")
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine:sid", {"yzRobo": {"ending_rp": 12264, "rp_change": -46}}, hint))
    assert db.rows[0]["rp_change"] == -46
    assert all(r["pubs_map"] == "Storm Point" and r["ranked_map"] == "E-District"
               for r in db.rows)  # per match: every row of the survivor


def test_surviving_copy_keeps_its_own_maps():
    t0 = time.time()
    db = _FakeDB(_session("other:sid", t0 - 20))
    for r in db.rows:
        r["pubs_map"], r["ranked_map"] = "Olympus", "Olympus"
    hint = _hint(t0)
    hint["yzRobo"].update(pubs_map="Storm Point", ranked_map="E-District")
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine:sid", {"yzRobo": {"rp_change": -46}}, hint))
    assert all(r["pubs_map"] == "Olympus" for r in db.rows)


def test_map_copy_failure_does_not_block_rp():
    t0 = time.time()
    db = _FakeDB(_session("other:sid", t0 - 20))  # rows have no map columns at all

    class _NoMapDB(_FakeDB):
        def table(self, name):
            q = super().table(name)
            orig = q.update

            def update(payload):
                if "pubs_map" in payload:
                    raise Exception("Could not find the 'pubs_map' column")
                return orig(payload)
            q.update = update
            return q
    db = _NoMapDB(db.rows)
    hint = _hint(t0)
    hint["yzRobo"].update(pubs_map="Storm Point", ranked_map="E-District")
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine:sid", {"yzRobo": {"rp_change": -46}}, hint))
    assert db.rows[0]["rp_change"] == -46


def test_old_csv_gains_map_columns():
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "apex_matches.csv")
        old = [c for c in at.CSV_FIELDS if c not in ("pubs_map", "ranked_map")]
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(",".join(old) + "\n")
            f.write(",".join("1" if c == "kills" else "" for c in old) + "\n")
        at._ensure_canonical_header(p)
        with open(p, encoding="utf-8") as f:
            header = f.readline().strip().split(",")
        assert header == at.CSV_FIELDS
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


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
