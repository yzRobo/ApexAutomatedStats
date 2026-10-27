"""Unit tests for the v1.6.8 reliability fixes in apex_tracker / rank_tracker.

Runs under pytest, or standalone with the project venv:
``.venv\\Scripts\\python.exe test_scanner_reliability.py``. Most tests use fakes and
need no OCR; the frame tests run the real reader and skip when a local-only frame
(not committed: it shows squadmates' account ids) is absent.

Covers: placement anchored to the total-kills number (#10-#20 with a 2-digit
total), total kills filled from player kills when the lone gold '1' won't OCR,
full-width digits in the revive/respawn vote, the native-16:9 row migration, read
reasons + the unlogged-summary episode, the CSV side file when the CSV is locked,
the RP patch fallback for a deleted cross-PC copy, and rank-tracker health.
"""

import os
import shutil
import tempfile
import time

import apex_tracker as at

HERE = os.path.dirname(os.path.abspath(__file__))


def local(*parts):
    p = os.path.join(HERE, *parts)
    return p if os.path.exists(p) else None


def read_frame(path):
    import cv2
    frame = cv2.imread(path)
    cfg = at.load_config()
    eff, _ = at.apply_profile(cfg, frame.shape[1], frame.shape[0], None)
    return frame, eff, at.scaler(eff, frame.shape[1], frame.shape[0])


# --------------------------------------------------------------------------- #
# placement anchored to the total-kills number
# --------------------------------------------------------------------------- #
def test_placement_real_frames():
    cases = [(("samples", "20260201212152_1.jpg"), 1),      # win, 16 kills (2-digit)
             (("samples", "20260329145043_1.jpg"), 1),      # win, 21 kills
             (("samples", "20251229162452_1.jpg"), 1),      # win, 9 kills
             (("samples", "live_eliminated.png"), 14),
             (("samples", "capture_20261025_195205.png"), 1),
             # the 2026-08-09 game that was never logged: #10 with 13 kills read as 0
             (("debug", "2026-08-09", "capture_20260809_003214.png"), 10),
             (("debug", "2026-08-09", "New", "capture_20260809_192057.png"), 19)]
    for parts, exp in cases:
        p = local(*parts)
        if not p:
            print(f"    (skipped {parts[-1]}: not present)")
            continue
        frame, eff, sc = read_frame(p)
        got = at.read_squad_placed(frame, eff, sc)
        assert got == exp, (parts[-1], got, exp)


def test_placement_small_shift_keeps_configured_box():
    # A single-digit total moves the anchor only a few px: the configured box is
    # used unchanged, so today's correct reads stay byte-identical.
    p = local("samples", "20251229162452_1.jpg")
    if not p:
        return
    frame, eff, sc = read_frame(p)
    narrow = at.parse_int(at.ocr_gold_number(at.crop(frame, sc(eff["header"]["squad_placed"]))))
    assert at.read_squad_placed(frame, eff, sc) == narrow == 1


# --------------------------------------------------------------------------- #
# total kills filled from the players' kills
# --------------------------------------------------------------------------- #
def test_total_kills_filled_when_header_unread():
    p = local("samples", "capture_20261025_195205.png")
    if not p:
        return
    frame, eff, sc = read_frame(p)
    x, y, w, h = sc(eff["header"]["total_kills"])
    frame[y:y + h, x:x + w] = 20  # blank the header total (as a lone '1' reads)
    m = at.extract_match(frame, eff, sc)
    assert m["total_squad_kills"] == sum(pl["kills"] for pl in m["players"]) == 17


def test_total_kills_read_value_is_never_overridden():
    p = local("samples", "capture_20261025_195205.png")
    if not p:
        return
    frame, eff, sc = read_frame(p)
    assert at.extract_match(frame, eff, sc)["total_squad_kills"] == 17


# --------------------------------------------------------------------------- #
# full-width digits in the single-digit vote
# --------------------------------------------------------------------------- #
def test_single_digit_vote_folds_fullwidth():
    import numpy as np
    real = at._rec
    try:
        at._rec = lambda img, up=3: "２２２２２"
        assert at.ocr_field(np.full((30, 75, 3), 200, np.uint8), numeric=True, single=True) == "2"
        # superscripts pass str.isdigit() but parse_int drops them: never vote for one
        at._rec = lambda img, up=3: "²²²１"
        assert at.ocr_field(np.full((30, 75, 3), 200, np.uint8), numeric=True, single=True) == "1"
    finally:
        at._rec = real


def test_fullwidth_not_folded_in_multi_digit_reads():
    # '冏７' for a lone gold '1' must stay unreadable, not become a plausible 7
    assert at.parse_int(at.digitize("冏７")) is None


# --------------------------------------------------------------------------- #
# config migration
# --------------------------------------------------------------------------- #
def _profile_cfg(revive, respawn):
    return {"profiles": {"1920x1080-native-16x9": {"rows": {"revive": revive, "respawn": respawn}}}}


def test_migration_fixes_shipped_native_rows():
    cfg = at._migrate_config(_profile_cfg({"y": 628, "h": 34}, {"y": 704, "h": 34}))
    rows = cfg["profiles"]["1920x1080-native-16x9"]["rows"]
    assert rows["revive"] == {"y": 618, "h": 34} and rows["respawn"] == {"y": 688, "h": 34}


def test_migration_leaves_hand_calibrated_rows():
    cfg = at._migrate_config(_profile_cfg({"y": 625, "h": 34}, {"y": 704, "h": 34}))
    rows = cfg["profiles"]["1920x1080-native-16x9"]["rows"]
    assert rows["revive"] == {"y": 625, "h": 34} and rows["respawn"] == {"y": 704, "h": 34}


def test_migration_without_profile():
    assert at._migrate_config({"rows": {}}) == {"rows": {}}


# --------------------------------------------------------------------------- #
# read reasons (return values unchanged)
# --------------------------------------------------------------------------- #
def _match(**over):
    players = [{"player_slot": i, "name": n, "kills": 1, "assists": 0, "knocks": 1,
                "damage": 300, "revive_given": 0, "respawn_given": 0}
               for i, n in ((1, "yzRobo"), (2, "SaWaii"), (3, "UltVaultHunter"))]
    m = {"session_id": "2:1:2:abc", "squad_placed": 5, "total_squad_kills": 3, "players": players}
    m.update(over)
    return m


def _try(match, stable=None, frame_ts=None):
    real = at.extract_match
    status = {"logged_this_run": 0}
    try:
        at.extract_match = lambda frame, cfg, scale: match
        res = at._try_log_match(None, {}, None, "unused.csv", set(), status,
                                lambda e: None, None, stable=stable, frame_ts=frame_ts)
    finally:
        at.extract_match = real
    return res, status.get("read_reason")


def test_reason_unread_field():
    m = _match()
    m["players"][1]["kills"] = None
    assert _try(m) == ("incomplete", "unread: slot 2 kills")


def test_reason_unread_placement():
    assert _try(_match(squad_placed=None)) == ("incomplete", "unread: placement")


def test_reason_implausible():
    m = _match(squad_placed=0)
    m["players"][0]["kills"] = 71717
    assert _try(m) == ("incomplete", "implausible: placement 0, slot 1 kills 71717")


def test_reason_waiting_for_confirmation():
    res, reason = _try(_match(), stable={"fp": None, "ft": 0.0}, frame_ts=1.0)
    assert res == "incomplete" and reason.startswith("waiting for a newer frame")


# --------------------------------------------------------------------------- #
# unlogged-summary episode
# --------------------------------------------------------------------------- #
class _Rec:
    def __init__(self):
        self.events, self.lines = [], []

    def emit(self, e):
        self.events.append(e)

    def dbg(self, msg):
        self.lines.append(msg)


def _episode(tmp):
    import numpy as np
    rec, status = _Rec(), {}
    at.DEBUG_DIR, old = tmp, at.DEBUG_DIR
    return at._SummaryEpisode(status, rec.emit, rec.dbg), status, rec, old, np.zeros((20, 20, 3), np.uint8)


def test_episode_logged_leaves_no_report():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        status["read_reason"] = "waiting for a newer frame to confirm the read"
        ep.read(frame, "incomplete")
        ep.read(frame, "logged")
        ep.end("summary left the screen")
        assert status.get("missed") is None and not os.path.exists(os.path.join(tmp, "unlogged"))
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_episode_unlogged_saves_frame_and_reason():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        status["read_reason"] = "implausible: slot 1 kills 71717"
        ep.read(frame, "incomplete")
        ep.end("watch stopped", final=True)
        assert status["missed"]["reason"] == "implausible: slot 1 kills 71717"
        assert status["missed"]["frame"] and os.path.exists(status["missed"]["frame"])
        assert os.path.exists(status["missed"]["frame"][:-4] + ".json")
        assert "missed" in rec.events and any("SUMMARY NOT LOGGED" in l for l in rec.lines)
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_episode_tab_away_then_logged_is_not_a_miss():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        status["read_reason"] = "waiting for a newer frame to confirm the read"
        ep.read(frame, "incomplete")
        ep.end("summary left the screen")   # tabbed away: parked, not reported
        ep.end("summary left the screen")   # called again while still away
        assert status.get("missed") is None and ep.pending is not None
        ep.read(frame, "incomplete")        # back on the summary
        ep.read(frame, "logged")
        ep.end("watch stopped", final=True)
        assert status.get("missed") is None and ep.pending is None
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_episode_miss_reported_once_gone_long_enough():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        status["read_reason"] = "unread: placement"
        ep.read(frame, "incomplete")
        ep.end("summary left the screen")
        assert status.get("missed") is None
        ep.pending["left"] -= ep.CONFIRM_MISS + 1
        ep.end("summary left the screen")
        assert status["missed"]["reason"] == "unread: placement" and ep.pending is None
        assert rec.events.count("missed") == 1
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_episode_warns_while_summary_still_up():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        status["read_reason"] = "implausible: slot 1 kills 71717"
        ep.read(frame, "incomplete")
        ep.warn_since -= ep.WARN_AFTER + 1  # failing reads for > WARN_AFTER s
        status["read_reason"] = "implausible: slot 1 kills 7171"  # text varies per read
        ep.read(frame, "incomplete")
        assert status["unread_summary"] == "implausible: slot 1 kills 7171"
        assert "unread" in rec.events
        ep.read(frame, "duplicate")  # it logged after all: the warning clears
        assert status["unread_summary"] is None
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_episode_warning_timer_restarts_per_hold():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        status["read_reason"] = "waiting for a newer frame to confirm the read"
        ep.read(frame, "incomplete")
        ep.warn_since -= ep.WARN_AFTER + 1  # an on-demand idle gap went by
        ep.resume()                         # the next probe starts a new hold
        ep.read(frame, "incomplete")
        assert not status.get("unread_summary")
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


def test_loop_error_status_goes_inactive_after_a_clean_pass():
    status, events = {}, []
    try:
        raise RuntimeError("capture hiccup")
    except RuntimeError:
        at._report_loop_error(status, events.append, lambda m: None, 1)
    assert status["loop_error"]["active"] and status["loop_error"]["msg"].endswith("capture hiccup")
    at._loop_pass_ok(status)
    assert status["loop_error"]["active"] is False


def test_episode_without_text_confirmed_read_is_silent():
    tmp = tempfile.mkdtemp()
    ep, status, rec, old, frame = _episode(tmp)
    try:
        ep.end("summary left the screen")  # banner colour only, never a read
        assert status.get("missed") is None and not rec.events
    finally:
        at.DEBUG_DIR = old
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CSV side file while the CSV can't be written
# --------------------------------------------------------------------------- #
def test_locked_csv_spools_then_merges():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex_matches.csv")
    client, at._SUPABASE_CLIENT = at._SUPABASE_CLIENT, None
    try:
        os.mkdir(path)  # a directory can't be opened for append: stands in for a lock
        at.append_match(path, _match())  # must not raise
        spool = at._csv_spool_path(path)
        assert len(at._read_csv_rows(spool)) == 3
        assert any(k.startswith("stat:") for k in at.already_logged_ids(path))
        os.rmdir(path)
        m2 = _match(session_id="2:1:2:def", squad_placed=7)
        at.append_match(path, m2)
        rows = at._read_csv_rows(path)
        assert len(rows) == 6 and not os.path.exists(spool)
        assert [r["session_id"] for r in rows] == ["2:1:2:abc"] * 3 + ["2:1:2:def"] * 3
    finally:
        at._SUPABASE_CLIENT = client
        shutil.rmtree(tmp, ignore_errors=True)


def test_spool_merge_never_duplicates_rows():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex_matches.csv")
    client, at._SUPABASE_CLIENT = at._SUPABASE_CLIENT, None
    try:
        os.mkdir(path)
        at.append_match(path, _match())
        spool = at._csv_spool_path(path)
        saved = open(spool, encoding="utf-8").read()
        os.rmdir(path)
        at.append_match(path, _match(session_id="2:1:2:def"))
        open(spool, "w", encoding="utf-8").write(saved)  # as if the delete had failed
        at.append_match(path, _match(session_id="2:1:2:ghi"))
        sids = [r["session_id"] for r in at._read_csv_rows(path)]
        assert sids.count("2:1:2:abc") == 3 and len(sids) == 9, sids
    finally:
        at._SUPABASE_CLIENT = client
        shutil.rmtree(tmp, ignore_errors=True)


def test_rp_patch_reaches_rows_waiting_in_the_side_file():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex_matches.csv")
    client, at._SUPABASE_CLIENT = at._SUPABASE_CLIENT, None
    try:
        os.mkdir(path)  # locked
        at.append_match(path, _match())
        try:
            at._patch_rp_in_csv(path, "2:1:2:abc", {"yzRobo": {"ending_rp": 12000, "rp_change": -40}})
        except Exception:
            pass  # the locked main file reports its failure; the side file is still patched
        os.rmdir(path)
        at.append_match(path, _match(session_id="2:1:2:def"))
        row = next(r for r in at._read_csv_rows(path)
                   if r["session_id"] == "2:1:2:abc" and r["name"] == "yzRobo")
        assert row["rp_change"] == "-40" and row["ending_rp"] == "12000"
    finally:
        at._SUPABASE_CLIENT = client
        shutil.rmtree(tmp, ignore_errors=True)


class _FlakyInsertDB:
    """Fails the first `fail` inserts, then records every inserted batch."""
    def __init__(self, fail):
        self.fail, self.inserted = fail, []

    def table(self, name):
        db = self

        class T:
            def insert(self, rows, returning=None):
                class X:
                    def execute(self_):
                        if db.fail:
                            db.fail -= 1
                            raise ConnectionError("network down")
                        db.inserted.append(rows)
                return X()
        return T()


def test_failed_insert_is_queued_and_retried():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex_matches.csv")
    db = _FlakyInsertDB(fail=1)
    client, at._SUPABASE_CLIENT = at._SUPABASE_CLIENT, db
    try:
        assert at.append_match(path, _match()) == (True, False)
        assert os.path.exists(at._unsynced_path(path))
        assert at.append_match(path, _match(session_id="2:1:2:def")) == (True, True)
        # the new match goes first, then the one that failed earlier
        assert [b[0]["session_id"] for b in db.inserted] == ["2:1:2:def", "2:1:2:abc"]
        assert not os.path.exists(at._unsynced_path(path))
    finally:
        at._SUPABASE_CLIENT = client
        shutil.rmtree(tmp, ignore_errors=True)


def test_supabase_still_down_costs_one_attempt_per_match():
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "apex_matches.csv")
    db = _FlakyInsertDB(fail=99)
    client, at._SUPABASE_CLIENT = at._SUPABASE_CLIENT, db
    try:
        for i in range(3):
            at.append_match(path, _match(session_id=f"2:1:2:{i}"))
        assert db.fail == 99 - 3  # one attempt each: no retry storm on the watch thread
        with open(at._unsynced_path(path), encoding="utf-8") as f:
            assert len(f.readlines()) == 3
        db.fail = 0
        at.append_match(path, _match(session_id="2:1:2:ok"))
        assert [b[0]["session_id"] for b in db.inserted] == \
            ["2:1:2:ok", "2:1:2:0", "2:1:2:1", "2:1:2:2"]
    finally:
        at._SUPABASE_CLIENT = client
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------- #
# RP patch fallback for a cross-PC copy the DB dedup deleted
# --------------------------------------------------------------------------- #
class _Q:
    def __init__(self, db, table):
        self.db, self.filters, self.op, self.payload = db, [], "select", None

    def select(self, *_):
        self.op = "select"
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def _f(self, kind, col, val):
        self.filters.append((kind, col, val))
        return self

    def eq(self, c, v): return self._f("eq", c, v)
    def gte(self, c, v): return self._f("gte", c, v)
    def lte(self, c, v): return self._f("lte", c, v)
    def is_(self, c, v): return self._f("is", c, v)

    def _match(self, row):
        for kind, col, val in self.filters:
            v = row.get(col)
            if kind == "eq" and v != val:
                return False
            if kind == "is" and v is not None:
                return False
            if kind == "gte" and not (v >= val):
                return False
            if kind == "lte" and not (v <= val):
                return False
        return True

    def execute(self):
        hit = [r for r in self.db.rows if self._match(r)]
        if self.op == "update":
            for r in hit:
                r.update(self.payload)
        return type("R", (), {"data": [dict(r) for r in hit]})()


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, name):
        return _Q(self, name)


def _ts(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


SQUAD = [("yzRobo", 223, 1), ("UltVaultHunter", 343, 0), ("SaWaii", 1318, 3)]


def _session(sid, t, placed=11, tk=4, squad=SQUAD, first_id=1, names=None, rp=None):
    names = names or [n for n, _, _ in squad]
    return [{"id": first_id + i, "session_id": sid, "name": names[i], "damage": d, "kills": k,
             "squad_placed": placed, "total_squad_kills": tk, "timestamp": _ts(t),
             "rp_change": rp, "ending_rp": None} for i, (_, d, k) in enumerate(squad)]


def _hint(t0, placed=11, tk=4, squad=SQUAD, who="yzRobo"):
    rows = [{"damage": d, "kills": k} for _, d, k in squad]
    d, k = next((d, k) for n, d, k in squad if n == who)
    return {who: {"damage": d, "kills": k, "logged_epoch": t0,
                  "match_fp": at._match_fp(placed, tk, rows)}}


def _with_db(db, service, fn):
    client, svc = at._SUPABASE_CLIENT, at._SUPABASE_IS_SERVICE
    at._SUPABASE_CLIENT, at._SUPABASE_IS_SERVICE = db, service
    try:
        fn()
    finally:
        at._SUPABASE_CLIENT, at._SUPABASE_IS_SERVICE = client, svc


def test_rp_patch_lands_on_surviving_copy():
    t0 = time.time()
    db = _FakeDB(_session("other:sid", t0 - 20))  # only the other PC's copy survived
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine:sid", {"yzRobo": {"ending_rp": 12264, "rp_change": -46}}, _hint(t0)))
    assert db.rows[0]["rp_change"] == -46 and db.rows[0]["ending_rp"] == 12264
    assert all(r["rp_change"] is None for r in db.rows[1:])


def test_rp_patch_survivor_with_misread_name_matched_by_stats():
    t0 = time.time()
    db = _FakeDB(_session("other", t0, names=["yzRob0", "UltVaultHunter", "SaWaii"]))
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -46}}, _hint(t0)))
    assert db.rows[0]["rp_change"] == -46


def test_rp_patch_old_build_null_total_still_matches():
    t0 = time.time()
    db = _FakeDB(_session("other", t0, tk=None))  # pre-1.6.8 copy stored NULL
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -46}}, _hint(t0, tk=4)))
    assert db.rows[0]["rp_change"] == -46


def test_rp_patch_never_lands_on_a_different_game():
    # Same player, same damage and kills, minutes apart - but a different match
    # (other placement / squadmates): the fingerprint must not match.
    t0 = time.time()
    other_game = [("yzRobo", 223, 1), ("Rotarynerd", 90, 0), ("SaWaii", 410, 1)]
    db = _FakeDB(_session("game2", t0 + 300, placed=15, tk=2, squad=other_game))
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -46}}, _hint(t0)))
    assert all(r["rp_change"] is None for r in db.rows)


def test_rp_patch_fallback_skips_ambiguous_and_filled():
    t0 = time.time()
    db = _FakeDB(_session("a", t0) + _session("b", t0 + 5, first_id=10))  # two copies
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -40}}, _hint(t0)))
    assert all(r["rp_change"] is None for r in db.rows)
    db2 = _FakeDB(_session("a", t0, rp=12))  # already resolved: never overwritten
    _with_db(db2, True, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -40}}, _hint(t0)))
    assert db2.rows[0]["rp_change"] == 12


def test_rp_patch_no_fallback_on_anon_key_or_old_record():
    t0 = time.time()
    db = _FakeDB(_session("other", t0))
    _with_db(db, False, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -40}}, _hint(t0)))
    old_record = {"yzRobo": {"damage": 223, "kills": 1, "logged_epoch": t0}}  # pre-1.6.8 queue
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine", {"yzRobo": {"rp_change": -40}}, old_record))
    assert all(r["rp_change"] is None for r in db.rows)


def test_rp_patch_own_row_still_updated_directly():
    t0 = time.time()
    db = _FakeDB(_session("mine", t0))
    _with_db(db, True, lambda: at._patch_rp_in_supabase(
        "mine", {"SaWaii": {"rp_change": -19}}, _hint(t0, who="SaWaii")))
    assert [r["rp_change"] for r in db.rows] == [None, None, -19]


# --------------------------------------------------------------------------- #
# rank tracker health
# --------------------------------------------------------------------------- #
def _tracker(started_ago, last_ok_ago=None, err=None, poll=30, contact_ago=None):
    import threading
    from rank_tracker import RankTracker
    rt = RankTracker.__new__(RankTracker)  # no thread, no network
    rt._lock, rt._poll_seconds = threading.Lock(), poll
    now = time.time()
    rt._started_ts = now - started_ago
    rt._last_ok_ts = None if last_ok_ago is None else now - last_ok_ago
    rt._last_contact_ts = None if contact_ago is None else now - contact_ago
    rt._last_error = err
    return rt


def test_health_states():
    assert _tracker(10).health() == ("starting", None)
    assert _tracker(500, last_ok_ago=20).health() == ("ok", None)
    assert _tracker(500, err="ALS rejected the API key (403)").health() == \
        ("failing", "ALS rejected the API key (403)")
    assert _tracker(900, last_ok_ago=600, err="ALS rate limit (429)").health()[0] == "failing"
    # ALS answers but nobody has ranked RP: not an outage
    assert _tracker(900, contact_ago=10).health()[0] == "ok"


def test_rp_state_off_without_tracker():
    real = at._RANK_TRACKER
    try:
        at._RANK_TRACKER = None
        assert at._rp_state()[0] == "off"
    finally:
        at._RANK_TRACKER = real


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
