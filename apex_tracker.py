"""
Apex Legends post-game summary tracker.

Passive, read-only screen reader: it captures the screen (same kind of OS
capture OBS uses), detects the gold CHAMPIONS / SUMMARY screen, OCR-reads each
player card + the match/session id, and appends rows to a CSV. It never touches
the Apex process, never reads game memory, and never sends input to the game.

Usage:
    py apex_tracker.py shot                 # save one capture to debug/ (prove capture works)
    py apex_tracker.py monitors             # list detected monitors
    py apex_tracker.py devices              # list video input devices (find the OBS Virtual Camera)
    py apex_tracker.py setup                # ask your resolution + save it to config.json
    py apex_tracker.py calibrate [img.png]  # draw crop boxes + OCR them (uses a PNG, or live screen)
    py apex_tracker.py watch                # run the live auto-watcher
    Any command also accepts --res WIDTHxHEIGHT to force a resolution profile,
    e.g. py apex_tracker.py calibrate shot.png --res 2560x1440

Config lives in config.json next to this file.
"""

import sys
import os
import csv
import json
import time
import re
import threading
import ctypes
import ctypes.wintypes as wt
from datetime import datetime, timezone
import traceback

try:
    from dotenv import load_dotenv
    from supabase import create_client, Client
except ImportError:
    load_dotenv = None
    create_client = None

from rank_tracker import RankTracker

import numpy as np
import cv2

# When frozen by PyInstaller, __file__ points inside a temp extract dir. The
# user-editable files (config.json, .env) and outputs (CSV, debug/) must live
# next to the .exe instead, so resolve our base dir from sys.executable.
if getattr(sys, "frozen", False):
    HERE = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
DEBUG_DIR = os.path.join(HERE, "debug")

__version__ = "1.5.0"
REPO = "yzRobo/ApexAutomatedStats"  # for the in-app update check


def latest_release_version(timeout=4):
    """Return the latest GitHub release tag (e.g. 'v1.2.0'), or None on failure.
    Stdlib only; used by the GUI's update check. Never raises."""
    import urllib.request
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "apex-tracker"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r).get("tag_name")
    except Exception:
        return None

# Initialize Supabase client if configured.
# Key preference: a SERVICE_ROLE key (the project owner's own machine — full
# access, lets roster upsert work) takes priority if present; otherwise the
# publishable/anon key, which is the ONLY key shipped to friends. The anon key
# is safe to distribute: RLS restricts it to inserting match rows (see
# supabase_rls.sql), so a leaked copy can't read, edit, or delete data.
_SUPABASE_CLIENT = None
_SUPABASE_IS_SERVICE = False
if load_dotenv and create_client:
    load_dotenv(os.path.join(HERE, ".env"))
    _url = os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
    _service_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    _anon_key = (os.environ.get("SUPABASE_KEY")
                 or os.environ.get("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY"))
    _key = _service_key or _anon_key
    _SUPABASE_IS_SERVICE = bool(_service_key)
    if _url and _key and "your_" not in _url and "your_" not in _key:
        try:
            _SUPABASE_CLIENT = create_client(_url, _key)
        except Exception as e:
            print(f"Warning: Failed to initialize Supabase client: {e}")

def _sync_ranks_to_supabase(snapshot):
    """Upsert the live ALS rank snapshot onto the roster table (owner only).

    Called after every rank-tracker poll cycle with ``{name: {current_rp,
    rank_name, rank_div, ...}}``. Writes current_rp/rank_tier/rank_division so the
    dashboard can show each player's CURRENT rank + RP without waiting for a
    ranked match. Best-effort: never raises (RankTracker also guards the call).

    Only the owner's service-role key may UPDATE roster; the insert-only anon key
    (friends' builds) can't, so skip there — one owner instance keeps it fresh.
    The existing name-only roster upsert won't clobber these columns.
    """
    if not _SUPABASE_CLIENT or not _SUPABASE_IS_SERVICE:
        return
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for name, entry in (snapshot or {}).items():
        rp = entry.get("current_rp")
        if rp is None:
            continue  # ALS didn't resolve this player this cycle — leave prior snapshot
        div = entry.get("rank_div")
        rows.append({
            "name": name,
            "current_rp": rp,
            "rank_tier": entry.get("rank_name") or None,
            # ALS rankDiv is 1..4 (1=I highest .. 4=IV lowest); 0 = none/unknown
            # (Master/Predator) -> store NULL.
            "rank_division": div if div else None,
            "rank_updated_at": now,
        })
    if not rows:
        return
    try:
        _SUPABASE_CLIENT.table("roster").upsert(rows, on_conflict="name").execute()
    except Exception as e:
        print(f"(Supabase) rank snapshot sync skipped: {e}")


# ALS rank tracker. Built lazily by init_rank_tracker(cfg) at watch START (not at
# import), so an API key / roster / UID entered in the app's Settings applies on
# Stop->Start with no app restart, and so importing this module has no network
# side effects.
_RANK_TRACKER = None
_RANK_TRACKER_SIG = None  # (key, names, uids, poll) the live tracker was built for
# If a match's RP never moves within the window, assume rp_change=0 (a no-RP /
# floor-protected loss) instead of null. Off by default (can't tell a genuine 0
# from a non-ranked match or slow propagation). config "als_assume_zero_on_timeout".
_RANK_ASSUME_ZERO = False
# Load .env once so the owner's ALS_API_KEY there is available as a FALLBACK to the
# config.json key (which the Settings UI edits). Friends ship no ALS key in .env.
if load_dotenv:
    load_dotenv(os.path.join(HERE, ".env"))


def _resolve_als_key(cfg):
    """ALS key from config.json (Settings-editable) first, then the .env
    ALS_API_KEY (the owner's existing setup). None when unset/placeholder."""
    key = (cfg.get("als_api_key") or os.environ.get("ALS_API_KEY") or "").strip()
    return key if key and "your_" not in key else None


def init_rank_tracker(cfg):
    """(Re)build the ALS rank tracker from the CURRENT config. Called at watch
    start so an API key / roster / UID entered in Settings applies on Stop->Start
    with no app restart. Idempotent: rebuilds only when those settings changed,
    and tears the tracker down if the key was cleared."""
    global _RANK_TRACKER, _RANK_TRACKER_SIG, _RANK_ASSUME_ZERO
    _RANK_ASSUME_ZERO = bool(cfg.get("als_assume_zero_on_timeout", False))
    key = _resolve_als_key(cfg)
    if not key:
        if _RANK_TRACKER is not None:
            try:
                _RANK_TRACKER.stop()
            except Exception:
                pass
        _RANK_TRACKER, _RANK_TRACKER_SIG = None, None
        return
    names = list(cfg.get("known_names", []))
    uids = dict(cfg.get("als_uids", {}) or {})
    poll = cfg.get("rank_poll_seconds", 120)
    sig = (key, tuple(names), tuple(sorted(uids.items())), poll)
    if _RANK_TRACKER is not None and sig == _RANK_TRACKER_SIG:
        return  # nothing relevant changed
    if _RANK_TRACKER is not None:
        try:
            _RANK_TRACKER.stop()
        except Exception:
            pass
    try:
        _RANK_TRACKER = RankTracker(api_key=key, known_names=names,
                                    poll_seconds=poll, uids=uids,
                                    on_poll=_sync_ranks_to_supabase)
        _RANK_TRACKER_SIG = sig
        print(f"(ALS) Rank tracker started for {len(names)} players "
              f"(poll every {poll}s).")
    except Exception as e:
        _RANK_TRACKER, _RANK_TRACKER_SIG = None, None
        print(f"Warning: Failed to start rank tracker: {e}")

# Keep OCR from grabbing every CPU core (which would hitch the game when it runs).
os.environ.setdefault("OMP_NUM_THREADS", "2")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    # utf-8-sig tolerates a BOM, which some editors / PowerShell add when saving.
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8-sig") as f:
        return json.load(f)


def save_config(cfg):
    """Write config.json back (preserves key order and the _comment help keys)."""
    with open(os.path.join(HERE, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")


def apply_profile(cfg, frame_w, frame_h, forced=None):
    """Pick the region set for the active resolution.

    Profiles live under cfg['profiles']['WIDTHxHEIGHT'] and each carries its own
    base_width/base_height plus region blocks (header/columns/rows/detect) measured
    at that native resolution. When the active resolution matches a profile, those
    regions are used (scaled by ~1.0); otherwise cfg is returned unchanged and the
    base 1920x1080 regions are scaled to the frame (the default path that already
    works for any 16:9 resolution). Returns (effective_cfg, active_key_or_None).

    `forced` (e.g. "2560x1440", from config force_resolution or --res) overrides the
    detected resolution when choosing the profile key.
    """
    profiles = cfg.get("profiles") or {}
    key = forced or f"{frame_w}x{frame_h}"
    prof = profiles.get(key)
    if not prof:
        return cfg, None
    eff = dict(cfg)
    eff.update(prof)  # profile supplies base_width/height + region blocks
    return eff, key


def sync_roster_to_supabase(cfg):
    """Publish config.json `known_names` to the Supabase `roster` table so the
    dashboard can read the squad from a single source (this file). Best-effort:
    a failure here never blocks tracking. Requires the `roster` table to exist
    (see roster_schema.sql)."""
    if not _SUPABASE_CLIENT:
        return
    # upsert needs UPDATE rights, which the insert-only anon key (friends) lacks.
    # Only the project owner's service-role key can sync the roster; skip otherwise.
    if not _SUPABASE_IS_SERVICE:
        return
    names = [n.strip() for n in (cfg.get("known_names") or []) if n and n.strip()]
    if not names:
        return
    try:
        _SUPABASE_CLIENT.table("roster").upsert(
            [{"name": n} for n in names], on_conflict="name"
        ).execute()
        print(f"(Supabase) roster synced: {len(names)} players.")
    except Exception as e:
        print(f"(Supabase) roster sync skipped: {e}")


def scaler(cfg, frame_w, frame_h):
    """Return a function that scales base-1920x1080 boxes to the real frame size."""
    sx = frame_w / cfg["base_width"]
    sy = frame_h / cfg["base_height"]

    def scale(box):
        return (
            int(round(box["x"] * sx)),
            int(round(box["y"] * sy)),
            int(round(box["w"] * sx)),
            int(round(box["h"] * sy)),
        )

    return scale


def crop(frame, box_xywh):
    x, y, w, h = box_xywh
    h_img, w_img = frame.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(w_img, x + w), min(h_img, y + h)
    return frame[y0:y1, x0:x1]


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
_OCR = None


def get_ocr():
    global _OCR
    if _OCR is None:
        from rapidocr_onnxruntime import RapidOCR
        # Limit threads so OCR can't monopolise the CPU and hitch the game.
        _OCR = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
    return _OCR


# Apex uses a stylized font; isolated glyphs get misread as letters. These fields
# are strictly numeric, so map the common confusions back to digits.
DIGIT_MAP = {
    "O": "0", "o": "0", "D": "0", "Q": "0", "U": "0",
    # The Apex "0" is a rounded rectangle; OCR maps it to these box/circle glyphs.
    "口": "0", "□": "0", "■": "0", "〇": "0", "○": "0", "●": "0", "ロ": "0",
    "I": "1", "l": "1", "i": "1", "|": "1", "]": "1",
    "Z": "2", "A": "4", "S": "5", "G": "6", "b": "6",
    "T": "7", "B": "8", "g": "9", "q": "9",
}


def digitize(text):
    return "".join(DIGIT_MAP.get(c, c) for c in (text or ""))


def _rec(img_bgr, upscale=3):
    img = cv2.resize(img_bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    result, _ = get_ocr()(img, use_det=False, use_cls=False, use_rec=True)
    return " ".join(item[0] for item in result).strip() if result else ""


def ocr_field(img_bgr, numeric=False, single=False, upscale=3):
    """Recognition-only OCR for a tightly-cropped single field. Skipping the
    detection stage is far more reliable on these small fixed crops.

    single=True handles lone digits (revive/respawn): an isolated glyph has no
    context and is often misread, so we tile the crop horizontally and take the
    most common digit across the copies."""
    if img_bgr is None or img_bgr.size == 0:
        return ""
    if single:
        from collections import Counter
        # Lone glyphs lack context; tile horizontally and vote. Try the raw crop,
        # then a binarized version, at a couple of scales until digits appear.
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        _, binimg = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        binbgr = cv2.cvtColor(binimg, cv2.COLOR_GRAY2BGR)
        for variant in (img_bgr, binbgr):
            for up in (4, 3, 5):
                tiled = np.hstack([variant] * 5)
                digits = [c for c in digitize(_rec(tiled, up)) if c.isdigit()]
                if digits:
                    return Counter(digits).most_common(1)[0][0]
        return ""
    text = _rec(img_bgr, upscale)
    return digitize(text) if numeric else text


def ocr_session(img_bgr):
    """The id is a long thin string whose colons OCR sometimes merges. The band is
    tall (to cover letterboxed screenshots and full-screen live capture), so first
    crop down to the actual text row, then try a few upscales and keep the first
    that forms a valid 4-part id, else the best one."""
    if img_bgr is None or img_bgr.size == 0:
        return ""
    # Locate the bright text row within the band (white id on dark background).
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    bright = np.where(gray.max(axis=1) > 120)[0]
    if len(bright):
        y0 = max(0, bright[0] - 4)
        y1 = min(img_bgr.shape[0], bright[-1] + 5)
        img_bgr = img_bgr[y0:y1]
    best = ""
    for up in (4, 6, 3):
        cand = clean_session_id(_rec(img_bgr, up))
        if re.fullmatch(r"\d+:\d+:\d+:[0-9A-Za-z]+", cand):
            return cand
        if cand.count(":") > best.count(":") or (
                cand.count(":") == best.count(":") and len(cand) > len(best)):
            best = cand
    return best


def ocr_gold_number(img_bgr, upscale=4):
    """Header numbers (#placed, total kills) are gold on a busy bar next to white
    labels. Isolate the gold pixels so only the number remains, then read it."""
    if img_bgr is None or img_bgr.size == 0:
        return ""
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([15, 80, 90]), np.array([42, 255, 255]))
    iso = cv2.bitwise_and(img_bgr, img_bgr, mask=mask)
    return digitize(_rec(iso, upscale))


def ocr_white_number(img_bgr, upscale=3):
    """Damage is white digits, and the colored character art behind the card can
    bleed into the right of the box and add a phantom digit (e.g. '1,559' -> '15591').
    Keep only the bright (white) pixels so just the number remains, then read it.
    Position-independent, so it doesn't depend on the box width lining up exactly."""
    if img_bgr is None or img_bgr.size == 0:
        return ""
    gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
    _, mask = cv2.threshold(gray, 165, 255, cv2.THRESH_BINARY)
    iso = cv2.bitwise_and(img_bgr, img_bgr, mask=mask)
    return digitize(_rec(iso, upscale))


def ocr_detect(img_bgr, upscale=2):
    """Detection+recognition OCR, used to find the banner text on a wider region."""
    if img_bgr is None or img_bgr.size == 0:
        return ""
    img = cv2.resize(img_bgr, None, fx=upscale, fy=upscale, interpolation=cv2.INTER_CUBIC)
    result, _ = get_ocr()(img)
    if not result:
        return ""
    return " ".join(line[1] for line in result).strip()


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #
def parse_int(text):
    digits = re.sub(r"[^0-9]", "", text or "")
    return int(digits) if digits else None


def parse_kak(text):
    """'5 / 3 / 5' -> (5, 3, 5). Tolerates OCR noise around the slashes."""
    nums = re.findall(r"\d+", text or "")
    nums = [int(n) for n in nums[:3]]
    while len(nums) < 3:
        nums.append(None)
    return nums[0], nums[1], nums[2]


def snap_name(name, cfg):
    """Snap an OCR'd name to the closest recurring squadmate name, if close enough.
    Fixes stray-glyph misreads, and handles the game truncating long names behind
    the card art (so we also compare against each known name's matching-length
    prefix, e.g. OCR 'WhopperGot' vs 'WhopperGobbler'[:10] = 'WhopperGob')."""
    import difflib
    name = (name or "").strip()
    known = cfg.get("known_names") or []
    if not name or not known:
        return name
    cutoff = cfg.get("name_match_cutoff", 0.8)
    best, best_r = name, 0.0
    for k in known:
        r = max(difflib.SequenceMatcher(None, name, k).ratio(),
                difflib.SequenceMatcher(None, name, k[:len(name)]).ratio())
        if r > best_r:
            best, best_r = k, r
    return best if best_r >= cutoff else name


def clean_session_id(text):
    # The id looks like '4:1000209:10827687:a00314cd' (colon-separated parts).
    # Pull that token out of any surrounding OCR noise; require >=2 colons.
    compact = re.sub(r"\s+", "", text or "")
    m = re.search(r"[0-9A-Za-z]+:[0-9A-Za-z]+:[0-9A-Za-z]+(?::[0-9A-Za-z]+)*", compact)
    if m:
        return m.group(0)
    return re.sub(r"[^0-9A-Za-z:]", "", compact)


# Upper bounds for a single player's per-match stats. Far above anything legitimately
# reachable in an Apex match (a huge game tops out well under these), so anything past
# them is an OCR misread - concatenated digits from a mid-animation or misaligned read.
_MAX_DAMAGE = 25000
_MAX_COUNT = 75  # kills / assists / knocks / revives / respawns


def _stats_plausible(match):
    """True if every player's stats are within physically-possible bounds. A read
    that produces junk like 976561 damage or 1001 assists fails this, so the caller
    can retry instead of logging garbage. Squad placement is 1-20."""
    sp = match.get("squad_placed")
    if sp is not None and not (1 <= sp <= 20):
        return False
    for p in match["players"]:
        if (p.get("damage") or 0) > _MAX_DAMAGE:
            return False
        for k in ("kills", "assists", "knocks", "revive_given", "respawn_given"):
            if (p.get(k) or 0) > _MAX_COUNT:
                return False
    return True


def fingerprint(players):
    return "fp:" + "_".join(sorted(f"{p['name']}{p['damage']}" for p in players))


def match_key(match):
    """Value stored in the session_id column. Real id if present, else a name+damage
    fingerprint so a no-id screen still gets a stable identifier."""
    sid = match.get("session_id") or ""
    if len(sid) >= 6:
        return sid
    return fingerprint(match["players"])


def stat_fingerprint(players, squad_placed=None, total_squad_kills=None):
    """Primary dedup key, built from the stable, reliably-OCR'd values: each player's
    name + K/A/Kn/damage/revives/respawns, plus placement and total squad kills. The
    bottom-left session id OCRs inconsistently (a dropped char or merged colon), so it
    can't be trusted for dedup; these stat fields do not. Accepts player dicts from a
    live match or rows read back from the CSV (values may be ints or numeric strings;
    both format identically)."""
    def g(p, k):
        v = p.get(k, 0)
        return "" if v is None else str(v)
    parts = sorted(
        "|".join(g(p, k) for k in
                 ("name", "kills", "assists", "knocks", "damage",
                  "revive_given", "respawn_given"))
        for p in players)
    sp = "" if squad_placed is None else str(squad_placed)
    tk = "" if total_squad_kills is None else str(total_squad_kills)
    return f"stat:{sp}|{tk}|" + "_".join(parts)


def sid_norm(session_id):
    """Secondary dedup key: the session id with punctuation stripped, or None when
    there's no usable real id (a fingerprint placeholder or too-short read)."""
    sid = session_id or ""
    if sid.startswith("fp:"):
        return None
    norm = re.sub(r"[^0-9A-Za-z]", "", sid)
    return "sid:" + norm if len(norm) >= 8 else None


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _exe_of(hwnd):
    pid = wt.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wt.DWORD(512)
        if _kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value).lower()
        return ""
    finally:
        _kernel32.CloseHandle(h)


def find_window_hwnd(exe_names):
    """Return the HWND of the first visible window owned by one of exe_names."""
    wanted = {e.lower() for e in exe_names}
    found = [None]
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wt.HWND, wt.LPARAM)

    def cb(hwnd, _):
        if _user32.IsWindowVisible(hwnd) and _exe_of(hwnd) in wanted:
            found[0] = hwnd
            return False
        return True

    _user32.EnumWindows(proto(cb), 0)
    return found[0]


def _monitor_rects():
    rects = []
    proto = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.POINTER(wt.RECT), wt.LPARAM)

    def cb(hmon, hdc, lprc, _):
        r = lprc.contents
        rects.append((r.left, r.top, r.right, r.bottom))
        return True

    _user32.EnumDisplayMonitors(0, 0, proto(cb), 0)
    return rects


def set_below_normal_priority():
    """Drop our process priority so Windows always favours the game."""
    try:
        _kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        _kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        _kernel32.SetPriorityClass.restype = ctypes.c_int
        BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
        return bool(_kernel32.SetPriorityClass(_kernel32.GetCurrentProcess(),
                                               BELOW_NORMAL_PRIORITY_CLASS))
    except Exception:
        return False


def find_window_monitor_index(hwnd):
    """1-based index (for windows-capture) of the monitor the window sits on."""
    rect = wt.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    for i, (l, t, r, b) in enumerate(_monitor_rects()):
        if l <= cx < r and t <= cy < b:
            return i + 1
    return None


class WGCCapture:
    """Windows Graphics Capture (same API OBS uses). Captures a specific window
    (by HWND) or a monitor, in a background thread. Works with exclusive
    fullscreen and is light on the game. grab() returns the latest BGR frame."""

    def __init__(self, hwnd=None, monitor_index=None, throttle_ms=250):
        from windows_capture import WindowsCapture
        self._lock = threading.Lock()
        self._latest = None
        self._last_ts = 0.0
        self._cap = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            minimum_update_interval=throttle_ms,
            monitor_index=monitor_index,
            window_hwnd=hwnd,
        )

        @self._cap.event
        def on_frame_arrived(frame, capture_control):
            buf = frame.frame_buffer[:, :, :3].copy()
            with self._lock:
                self._latest = buf
                self._last_ts = time.time()

        @self._cap.event
        def on_closed():
            pass

        self._control = self._cap.start_free_threaded()

    def grab(self):
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def last_frame_age(self):
        with self._lock:
            return time.time() - self._last_ts if self._last_ts else 1e9

    def wait_first(self, timeout=5.0):
        t0 = time.time()
        while self.grab() is None and time.time() - t0 < timeout:
            time.sleep(0.05)
        return self.grab() is not None

    def release(self):
        try:
            self._control.stop()
        except Exception:
            pass


def find_video_device_index(name_hint="OBS Virtual"):
    """Return the index of the first video input device whose name contains
    name_hint (default the OBS Virtual Camera), or None. Uses pygrabber's
    DirectShow enumeration; returns None if pygrabber isn't available or the
    device isn't present (e.g. OBS isn't running with Virtual Camera started)."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
    except Exception:
        return None
    hint = name_hint.lower()
    for i, n in enumerate(names):
        if hint in (n or "").lower():
            return i
    return None


class VideoDeviceCapture:
    """Reads a DirectShow video device (e.g. the OBS Virtual Camera) on a
    background thread, exposing the same grab()/last_frame_age()/wait_first()/
    release() interface as WGCCapture.

    This is the ZERO game-overhead path: OBS captures Apex with its own Game
    Capture hook (smooth, already running) and outputs the frames as a virtual
    camera; we just read that camera. We never touch the game, so it stays
    passive and anti-cheat-safe, and the game keeps full exclusive-fullscreen
    performance because nothing here captures the screen."""

    def __init__(self, index, width=1920, height=1080, read_interval=0.05):
        self._lock = threading.Lock()
        self._latest = None
        self._last_ts = 0.0
        self._stop = threading.Event()
        self._read_interval = read_interval
        # CAP_DSHOW is the reliable backend for the OBS virtual cam on Windows.
        self._cap = cv2.VideoCapture(index, cv2.CAP_DSHOW)
        try:
            # Request the OBS canvas resolution. Without this, DirectShow defaults
            # to 640x480 - far too small (and 4:3) for the summary-screen OCR.
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # keep latency low
        except Exception:
            pass
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self):
        while not self._stop.is_set():
            ok, frame = self._cap.read()
            if ok and frame is not None:
                with self._lock:
                    self._latest = frame
                    self._last_ts = time.time()
            else:
                time.sleep(0.1)  # device not delivering yet; back off
            self._stop.wait(self._read_interval)

    def grab(self):
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def last_frame_age(self):
        with self._lock:
            return time.time() - self._last_ts if self._last_ts else 1e9

    def wait_first(self, timeout=5.0):
        t0 = time.time()
        while self.grab() is None and time.time() - t0 < timeout:
            time.sleep(0.05)
        return self.grab() is not None

    def release(self):
        self._stop.set()
        try:
            self._cap.release()
        except Exception:
            pass


def enumerate_monitors():
    """Best-effort monitor list for the 'monitors' command, via dxcam."""
    try:
        import dxcam
    except Exception:
        return []
    outs, idx = [], 0
    while idx <= 8:
        try:
            cam = dxcam.create(device_idx=0, output_idx=idx)
        except Exception:
            cam = None
        if cam is None:
            break
        try:
            frame = cam.grab()
            size = (frame.shape[1], frame.shape[0]) if frame is not None else ("?", "?")
        finally:
            cam.release()
        outs.append({"index": idx, "size": size})
        idx += 1
    return outs


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
def banner_color_present(frame, cfg, scale):
    """Cheap, OCR-free check: is the gold (win) or red (loss) banner bar present?
    Used as a gate so OCR never runs during normal gameplay."""
    d = cfg["detect"]
    region = crop(frame, scale(d["banner"]))
    if region.size == 0:
        return False
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    gold_ratio = float(cv2.inRange(hsv, np.array(d["gold_hsv_min"]),
                                   np.array(d["gold_hsv_max"])).mean()) / 255.0
    red = (cv2.inRange(hsv, np.array(d["red_hsv_min1"]), np.array(d["red_hsv_max1"]))
           | cv2.inRange(hsv, np.array(d["red_hsv_min2"]), np.array(d["red_hsv_max2"])))
    red_ratio = float(red.mean()) / 255.0
    return gold_ratio >= d.get("gold_min_ratio", 0.02) or red_ratio >= d.get("red_min_ratio", 0.12)


def banner_text_present(frame, cfg, scale):
    """Confirm via OCR that the banner is a summary banner (CHAMPIONS / SQUAD
    ELIMINATED). Only called after the cheap color gate passes."""
    d = cfg["detect"]
    text = ocr_detect(crop(frame, scale(d["banner"]))).upper().replace(" ", "")
    return any(k in text for k in d.get("match_texts", ["CHAMPION", "ELIMINAT", "SQUAD", "SUMMAR"]))


def is_summary_screen(frame, cfg, scale):
    """Full check (color gate + OCR confirm). Used by calibrate / batch."""
    return banner_color_present(frame, cfg, scale) and banner_text_present(frame, cfg, scale)


def ranked_badge_present(frame, cfg, scale):
    """Cheap, OCR-free check for the ornate rank badge in the top-right gameplay
    HUD. The summary screen carries NO rank/RP marker, so we read 'is this a
    ranked match' off the live HUD badge during play and latch it for the match.

    Detects the *presence* of the badge, not which rank: the metallic emblem +
    division pips are edge-dense, whereas the sky/terrain that sits there in pubs
    is nearly edge-free (saturation is useless here — blue sky out-saturates the
    badge). A single Canny edge-density read on a small fixed crop, gated by the
    default-off ``ranked_detect_enabled`` flag and confirmed across two samples by
    the caller, so a one-frame busy background can't false-trigger.
    """
    d = cfg["detect"]
    box = d.get("ranked_badge")
    if not box:
        return False
    region = crop(frame, scale(box))
    if region.size == 0:
        return False
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    edge_density = float(cv2.Canny(gray, 80, 160).mean()) / 255.0
    return edge_density >= d.get("ranked_edge_min", 0.12)


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def extract_match(frame, cfg, scale):
    header = cfg["header"]
    session_id = ocr_session(crop(frame, scale(header["session_id"])))
    squad_placed = parse_int(ocr_gold_number(crop(frame, scale(header["squad_placed"]))))
    total_kills = parse_int(ocr_gold_number(crop(frame, scale(header["total_kills"]))))

    rows = cfg["rows"]
    value_w = cfg.get("value_width", 150)
    single_w = cfg.get("single_width", 75)
    players = []
    for slot, col in enumerate(cfg["columns"], start=1):
        def field(row_key, width):
            box = {"x": col["x"], "y": rows[row_key]["y"], "w": width, "h": rows[row_key]["h"]}
            return crop(frame, scale(box))

        name = snap_name(ocr_field(field("name", col["w"])), cfg)
        kills, assists, knocks = parse_kak(ocr_field(field("kak", value_w), numeric=True))
        damage = parse_int(ocr_white_number(field("damage", value_w)))
        revive = parse_int(ocr_field(field("revive", single_w), numeric=True, single=True))
        respawn = parse_int(ocr_field(field("respawn", single_w), numeric=True, single=True))

        players.append({
            "player_slot": slot,
            "name": name,
            "kills": kills,
            "assists": assists,
            "knocks": knocks,
            "damage": damage,
            "revive_given": revive,
            "respawn_given": respawn,
        })

    return {
        "session_id": session_id,
        "squad_placed": squad_placed,
        "total_squad_kills": total_kills,
        "players": players,
    }


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
CSV_FIELDS = [
    "timestamp", "session_id", "squad_placed", "total_squad_kills",
    "player_slot", "name", "kills", "assists", "knocks", "damage",
    "revive_given", "respawn_given",
    "starting_rp", "ending_rp", "rp_change",
    # Match-level: True/False once ranked detection (default-off) has run, else
    # blank. Appended last so it never shifts the columns of pre-existing CSVs.
    "ranked",
]

# Serializes all writes to the CSV. append_match() runs on the watch loop while
# the async RP-resolution threads rewrite the file to patch ending_rp; without
# this lock a rewrite could clobber a freshly-appended match (or two rewrites
# could lose each other's patches).
_CSV_LOCK = threading.Lock()


def csv_path(cfg):
    """Resolve the CSV target. With new_file_each_run, use a timestamped file per
    watch session; otherwise a single running file that accumulates all matches."""
    p = cfg.get("csv_path", "apex_matches.csv")
    if cfg.get("new_file_each_run"):
        root, ext = os.path.splitext(p)
        p = f"{root}_{datetime.now():%Y%m%d_%H%M%S}{ext or '.csv'}"
    return p if os.path.isabs(p) else os.path.join(HERE, p)


def already_logged_ids(path):
    """Dedup keys for matches already in the CSV. For each match (its 3 rows share a
    session_id) it adds both the stat fingerprint and the normalized session id, so a
    match already logged - even under a differently-OCR'd id - is recognized. Count
    distinct matches via the 'stat:' keys."""
    seen = set()
    if not os.path.exists(path):
        return seen
    groups = {}
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            groups.setdefault(row.get("session_id") or "", []).append(row)
    for sid, rows in groups.items():
        sk = sid_norm(sid)
        if sk:
            seen.add(sk)
        seen.add(stat_fingerprint(rows, rows[0].get("squad_placed"),
                                  rows[0].get("total_squad_kills")))
    return seen


def _ensure_canonical_header(path):
    """Bring an existing CSV up to the current CSV_FIELDS schema, in place.

    The schema grows over time (the `ranked` column was the latest add). A file
    written under an older, shorter header becomes RAGGED once wider rows are
    appended to it — the extra trailing values have no header column, so
    csv.DictReader stuffs them under a None key and the next rewrite dies with
    'dict contains fields not in fieldnames: None'. This rewrites the file once
    with the full CSV_FIELDS header: existing columns are matched by name, missing
    ones are padded blank, and any ragged trailing values are recovered positionally
    into the new columns (so an already-appended `ranked` value is preserved).

    Idempotent and cheap when the header already matches. The caller MUST already
    hold _CSV_LOCK — this does not acquire it (the lock isn't reentrant)."""
    if not os.path.exists(path):
        return
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows or rows[0] == CSV_FIELDS:
        return  # empty or already canonical
    header = rows[0]
    # Columns CSV_FIELDS adds beyond the old header, in order — these receive any
    # ragged values that earlier wider appends wrote with no header column.
    trailing_new = [c for c in CSV_FIELDS if c not in header]
    out = []
    for data in rows[1:]:
        if not data:
            continue
        d = {col: data[i] for i, col in enumerate(header) if i < len(data)}
        for col, val in zip(trailing_new, data[len(header):]):
            d[col] = val
        out.append(d)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for d in out:
            w.writerow({k: d.get(k, "") for k in CSV_FIELDS})


def append_match(path, match):
    new_file = not os.path.exists(path)
    # Timezone-aware local time: .astimezone() attaches this PC's UTC offset, so the
    # Supabase TIMESTAMPTZ column converts it to the CORRECT UTC instant. Without the
    # offset, a naive datetime.now() gets stored AS-IF it were UTC, so two squadmates
    # in different timezones logged the same match an hour apart. The CSV now shows
    # local time with its offset (e.g. ...-04:00), which is unambiguous too.
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    supabase_rows = []
    with _CSV_LOCK:
        # Upgrade a pre-`ranked` (or otherwise older) file to the full header first,
        # so appending wider rows can't produce a ragged file the patch step chokes on.
        if not new_file:
            _ensure_canonical_header(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if new_file:
                w.writeheader()

            # Only attach `ranked` when detection actually ran (True/False). Left
            # None when the default-off feature is disabled, so the key is omitted
            # from the Supabase insert entirely — a project that hasn't run
            # supabase_migration_ranked.sql keeps syncing fine (no PGRST204).
            ranked = match.get("ranked")
            for p in match["players"]:
                row_data = {
                    "timestamp": ts,
                    "session_id": match["session_id"],
                    "squad_placed": match["squad_placed"],
                    "total_squad_kills": match["total_squad_kills"],
                    **p,
                }
                if ranked is not None:
                    row_data["ranked"] = ranked
                w.writerow(row_data)
                supabase_rows.append(row_data)

    # Network sync runs outside the CSV lock so it can't block RP-patch threads.
    if _SUPABASE_CLIENT:
        def _insert(rows):
            # returning="minimal" avoids reading the rows back, so this works
            # under an insert-only RLS policy (no SELECT granted to anon).
            _SUPABASE_CLIENT.table("apex_matches").insert(
                rows, returning="minimal").execute()
        try:
            _insert(supabase_rows)
            print(f"[{datetime.now():%H:%M:%S}] (Supabase) synced {len(supabase_rows)} player records.")
        except Exception as e:
            # If the project hasn't run supabase_migration_ranked.sql, the `ranked`
            # column is missing and PostgREST rejects the WHOLE insert (PGRST204).
            # Don't lose the match over a not-yet-applied migration: strip `ranked`
            # and retry so the core stats still sync (the flag stays CSV-only until
            # the column exists).
            if "ranked" in str(e) and any("ranked" in r for r in supabase_rows):
                stripped = [{k: v for k, v in r.items() if k != "ranked"}
                            for r in supabase_rows]
                try:
                    _insert(stripped)
                    print(f"[{datetime.now():%H:%M:%S}] (Supabase) synced {len(stripped)} "
                          f"records WITHOUT 'ranked' - run supabase_migration_ranked.sql "
                          f"to store the ranked flag.")
                except Exception as e2:
                    print(f"[{datetime.now():%H:%M:%S}] (Supabase) Error syncing to Supabase: {e2}")
            else:
                print(f"[{datetime.now():%H:%M:%S}] (Supabase) Error syncing to Supabase: {e}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def open_live_capture(cfg, throttle_override=None):
    """Open the live capture.

    mode "monitor" (default): capture the whole monitor Apex is on. This is the
    smooth path for exclusive-fullscreen games (what OBS display capture uses).
    mode "window": capture just the Apex window (works on any monitor but can make
    a fullscreen game stutter).
    mode "obs": read the OBS Virtual Camera instead of capturing the screen at all
    (zero game overhead - OBS already has the frames). Requires OBS running with a
    Game Capture of Apex and Virtual Camera started.

    Returns (capture, description, hwnd). hwnd is the tracked Apex window (or None).
    """
    cap_cfg = cfg.get("capture", {})
    exe_names = cap_cfg.get("exe_names", ["r5apex_dx12.exe", "r5apex.exe"])
    # throttle_override lets the on-demand summary-read open a FAST session on the
    # static end screen (so the stability gate's confirming frame arrives sub-second)
    # without changing the gameplay throttle. None = use the configured throttle_ms.
    throttle = (throttle_override if throttle_override is not None
                else cap_cfg.get("throttle_ms", 500))
    mode = cap_cfg.get("mode", "monitor")
    hwnd = find_window_hwnd(exe_names)

    if mode == "obs":
        # Read OBS's Virtual Camera. No screen capture, so the game keeps full
        # exclusive-fullscreen performance and there is no stutter from us at all.
        idx = cap_cfg.get("video_device_index", -1)
        if idx is None or idx < 0:
            idx = find_video_device_index(cap_cfg.get("video_device_name", "OBS Virtual"))
        vw = cap_cfg.get("video_width", 1920)
        vh = cap_cfg.get("video_height", 1080)
        if idx is None:
            # Device not found - surface it clearly instead of silently capturing
            # nothing. The watch loop will report "waiting" and the debug log says why.
            return VideoDeviceCapture(-1, vw, vh), "OBS Virtual Camera (NOT FOUND - is OBS running with Virtual Camera started?)", hwnd
        return VideoDeviceCapture(idx, vw, vh), f"OBS Virtual Camera (device {idx})", hwnd

    if mode == "window" and hwnd:
        return WGCCapture(hwnd=hwnd, throttle_ms=throttle), "Apex window", hwnd

    # monitor mode (default): capture the monitor Apex is on.
    mon = None
    if hwnd:
        mon = find_window_monitor_index(hwnd)
    if mon is None:
        mon = cap_cfg.get("monitor_index", 1)
        note = "" if hwnd else " (Apex not found yet)"
        return WGCCapture(monitor_index=mon, throttle_ms=throttle), f"monitor {mon}{note}", hwnd
    return WGCCapture(monitor_index=mon, throttle_ms=throttle), f"monitor {mon} (Apex's monitor)", hwnd


def cmd_monitors():
    mons = enumerate_monitors()
    print("Detected monitors (index : resolution):")
    for o in mons:
        print(f"  {o['index']} : {o['size'][0]}x{o['size'][1]}")
    apex = find_window_hwnd(["r5apex_dx12.exe", "r5apex.exe"])
    print(f"\nApex window: {'found (hwnd %d)' % apex if apex else 'NOT running'}")


def cmd_devices():
    """List DirectShow video input devices (for picking video_device_index in OBS
    mode). The OBS Virtual Camera only appears once OBS has started it."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        names = FilterGraph().get_input_devices()
    except Exception as e:
        print(f"Could not enumerate video devices ({e}).")
        print("Install pygrabber:  py -m pip install pygrabber")
        return
    print("Video input devices (index : name):")
    for i, n in enumerate(names):
        marker = "  <-- OBS Virtual Camera" if "obs virtual" in (n or "").lower() else ""
        # Encode-safe print: device names can contain characters the console can't show.
        safe = (n or "").encode("ascii", "replace").decode("ascii")
        print(f"  {i} : {safe}{marker}")
    if not any("obs virtual" in (n or "").lower() for n in names):
        print("\nOBS Virtual Camera not listed. In OBS click 'Start Virtual Camera', then re-run.")


def cmd_batch(arg, forced_res=None):
    """Process image file(s) into a CSV for verification. arg is a folder, a
    glob, or a single image. Writes <csv>_samplecheck next to the normal csv."""
    import glob as _glob
    cfg = load_config()
    forced = forced_res or cfg.get("force_resolution") or None
    if arg and os.path.isdir(arg):
        paths = sorted(_glob.glob(os.path.join(arg, "*.jpg")) + _glob.glob(os.path.join(arg, "*.png")))
    elif arg and any(ch in arg for ch in "*?"):
        paths = sorted(_glob.glob(arg))
    elif arg:
        paths = [arg]
    else:
        paths = sorted(_glob.glob(os.path.join(HERE, "samples", "*.jpg")))

    out = os.path.join(DEBUG_DIR, "sample_check.csv")
    os.makedirs(DEBUG_DIR, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["source_image"] + CSV_FIELDS)
        w.writeheader()
        for path in paths:
            frame = cv2.imread(path)
            if frame is None:
                print(f"skip (unreadable): {path}")
                continue
            h, wd = frame.shape[:2]
            cfg_eff, _ = apply_profile(cfg, wd, h, forced)
            scale = scaler(cfg_eff, wd, h)
            match = extract_match(frame, cfg_eff, scale)
            if not match["session_id"]:
                match["session_id"] = match_key(match)  # show the fallback key
            name = os.path.basename(path)
            total_check = sum(p["kills"] or 0 for p in match["players"])
            print(f"\n{name}  session={match['session_id'] or '(none)'}  "
                  f"placed=#{match['squad_placed']}  total_kills={match['total_squad_kills']}  "
                  f"(sum of player kills={total_check})")
            for p in match["players"]:
                print(f"  {p['name']:16} K/A/Kn={p['kills']}/{p['assists']}/{p['knocks']}  "
                      f"dmg={p['damage']}  rev={p['revive_given']}  resp={p['respawn_given']}")
                w.writerow({"source_image": name, "timestamp": "", "session_id": match["session_id"],
                            "squad_placed": match["squad_placed"],
                            "total_squad_kills": match["total_squad_kills"], **p})
    print(f"\nWrote {out}")


def cmd_shot(mode_override=None):
    """Grab one frame through the live capture path and save it to debug/, so you
    can SEE what the tracker sees and confirm the feed isn't black/frozen. Pass
    mode_override (e.g. 'obs') to test a specific capture mode without editing
    config - handy for verifying the OBS Virtual Camera feed."""
    cfg = load_config()
    if mode_override:
        cfg.setdefault("capture", {})["mode"] = mode_override
    os.makedirs(DEBUG_DIR, exist_ok=True)
    cap, src, _ = open_live_capture(cfg)
    print(f"Capturing {src} ...")
    if not cap.wait_first(8):
        print("No frame received.")
        if (cfg.get("capture") or {}).get("mode") == "obs":
            print("  OBS mode: is OBS running with a Game Capture source AND "
                  "'Start Virtual Camera' clicked? Run 'devices' to confirm the camera exists.")
        else:
            print("  Is Apex running and visible?")
        cap.release()
        return
    frame = cap.grab()
    out = os.path.join(DEBUG_DIR, "capture_live.png")
    cv2.imwrite(out, frame)
    mb = float(frame.mean())
    black = mb < 2.0
    note = ("  <-- LOOKS BLACK (feed blocked / OBS scene empty)" if black
            else "  (looks good - open the PNG to confirm it shows your game)")
    print(f"saved {out}  {frame.shape[1]}x{frame.shape[0]}  mean_brightness={mb:.1f}{note}")
    cap.release()


def draw_boxes(frame, cfg, scale):
    out = frame.copy()
    def box(b, color, label):
        x, y, w, h = scale(b)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        cv2.putText(out, label, (x, max(0, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    box(cfg["detect"]["banner"], (0, 255, 255), "banner")
    if cfg["detect"].get("ranked_badge"):
        box(cfg["detect"]["ranked_badge"], (0, 128, 255), "ranked_badge")
    for key, b in cfg["header"].items():
        box(b, (255, 0, 255), key)
    for slot, col in enumerate(cfg["columns"], start=1):
        for rk, rv in cfg["rows"].items():
            box({"x": col["x"], "y": rv["y"], "w": col["w"], "h": rv["h"]},
                (0, 255, 0), f"{slot}:{rk}")
    return out


def cmd_calibrate(arg, forced_res=None):
    cfg = load_config()
    os.makedirs(DEBUG_DIR, exist_ok=True)
    if arg and os.path.exists(arg):
        frame = cv2.imread(arg)
        print(f"Loaded sample image {arg}  {frame.shape[1]}x{frame.shape[0]}")
    else:
        cap, src, _ = open_live_capture(cfg)
        print(f"Capturing {src} ...")
        frame = cap.grab() if cap.wait_first(8) else None
        cap.release()
        if frame is None:
            print("No frame captured and no valid image path given.")
            return
    h, w = frame.shape[:2]
    forced = forced_res or cfg.get("force_resolution") or None
    cfg, prof_key = apply_profile(cfg, w, h, forced)
    if prof_key:
        print(f"Region set: profile {prof_key}")
    else:
        print(f"Region set: scaling {cfg['base_width']}x{cfg['base_height']} base to {w}x{h}")
    scale = scaler(cfg, w, h)

    overlay = draw_boxes(frame, cfg, scale)
    overlay_path = os.path.join(DEBUG_DIR, "calibrate_overlay.png")
    cv2.imwrite(overlay_path, overlay)
    print(f"Wrote {overlay_path} — open it to check the boxes line up.\n")

    print(f"Summary screen detected: {is_summary_screen(frame, cfg, scale)}")
    match = extract_match(frame, cfg, scale)
    print(f"\nsession_id     : {match['session_id']!r}")
    print(f"squad_placed   : {match['squad_placed']}")
    print(f"total_kills    : {match['total_squad_kills']}")
    for p in match["players"]:
        print(f"  slot {p['player_slot']}: name={p['name']!r:24} "
              f"K/A/Kn={p['kills']}/{p['assists']}/{p['knocks']} "
              f"dmg={p['damage']} rev={p['revive_given']} resp={p['respawn_given']}")


def _try_log_match(frame, cfg_eff, scale, path, seen, status, emit, log_cb,
                   ranked=None, stable=None, frame_ts=None):
    """Read a settled summary frame and log it if it's new and fully rendered.
    Returns 'incomplete' (still animating - try again), 'duplicate' (already
    logged), or 'logged'. Shared by the persistent and on-demand watch loops so
    the dedup logic lives in exactly one place.

    ``stable``/``frame_ts`` drive the stability gate: the summary stats roll up
    when the screen first appears, so a single read can catch plausible-but-wrong
    mid-animation values (e.g. damage still climbing to its final number). We only
    commit once the SAME stats are confirmed by a NEWER capture frame - i.e. the
    numbers have stopped moving. ``stable`` is a mutable dict the caller keeps
    across reads ({"fp", "ft"}); ``frame_ts`` is the current frame's capture time.
    Omit both (calibrate/batch) to log immediately with no stability wait.

    ``ranked`` is the per-match latch from the HUD badge detector: True (ranked),
    False (pub), or None (detection disabled / never sampled). It is stored on the
    match and gates RP resolution — pubs skip the ALS polling entirely, which is
    also what makes ``als_assume_zero_on_timeout`` safe to enable. When detection
    is off (None) the prior always-resolve behaviour is preserved exactly."""
    match = extract_match(frame, cfg_eff, scale)
    # Wait until every player is named, every stat field read, AND placement has
    # rendered, so we never log a half-drawn screen. Names + placement alone is
    # not enough: a card whose K/A/Kn or damage row failed OCR returns None for
    # those, which earlier slipped through and logged blank-stat rows (e.g. a
    # player with empty kills/assists/knocks). Treat any None stat as "still
    # rendering" and retry. (None is distinct from a legit 0, which reads as 0.)
    def _row_incomplete(p):
        return (not p["name"] or p["kills"] is None or p["assists"] is None
                or p["knocks"] is None or p["damage"] is None)
    if any(_row_incomplete(p) for p in match["players"]) or match["squad_placed"] is None:
        return "incomplete"
    # Sanity gate: a mid-animation read (the stats roll up when the screen first
    # appears) or a misaligned region can yield physically-impossible values -
    # concatenated junk like 976561 damage or 1001 assists. Treat those as "not
    # done rendering" and retry, so garbage never gets logged. This also prevents
    # the double-log it caused: a garbage read has different stats AND a different
    # session-id than the good read, so dedup can't collapse the two.
    if not _stats_plausible(match):
        return "incomplete"
    if not match["session_id"]:
        match["session_id"] = match_key(match)  # persist so restarts dedup
    # Dedup on the stable stats, not the flaky session id. Skip if this match was
    # already logged - even if its id OCRs differently this time.
    sk = sid_norm(match["session_id"])
    fp = stat_fingerprint(match["players"], match["squad_placed"],
                          match["total_squad_kills"])
    if fp in seen or (sk and sk in seen):
        return "duplicate"

    # Stability gate: only log once these exact stats are confirmed, so a
    # mid-animation value (plausible but still rolling up) is never committed.
    # Confirm by EITHER a genuinely newer capture frame reproducing the same
    # fingerprint (the numbers stopped moving) OR — as a fallback when frames are
    # sparse (high throttle_ms) — the same values holding for a short wall-clock
    # window. Without the wall-clock fallback, a summary dismissed before the next
    # (up to throttle_ms-spaced) frame arrives is read correctly and then thrown
    # away; the fallback lets it commit after one extra poll instead.
    if stable is not None:
        now = time.time()
        if fp != stable.get("fp"):
            # New / changed read — (re)start the confirmation window.
            stable["fp"] = fp
            stable["ft"] = frame_ts if frame_ts is not None else 0.0
            stable["seen"] = now
            return "incomplete"
        fresh_frame = frame_ts is not None and frame_ts > stable.get("ft", 0.0)
        if frame_ts is not None:
            stable["ft"] = frame_ts
        confirm_secs = cfg_eff.get("stability_confirm_seconds", 0.8)
        if not (fresh_frame or (now - stable.get("seen", now)) >= confirm_secs):
            return "incomplete"

    match["ranked"] = ranked
    # RP only makes sense for ranked matches. With detection disabled (ranked is
    # None) we keep the original always-resolve behaviour. With it ENABLED, the
    # HUD-badge detector can still miss a genuine ranked game (short match, early
    # death, or a rank tier it wasn't calibrated on), which would silently blank
    # that match's RP. So unless assume-zero is on — where skipping pubs is exactly
    # what keeps a false 0 off them — we fail OPEN on an unconfirmed (False) flag:
    # still snapshot + queue resolution. A true pub then simply never moves RP and
    # stays blank (no harm), while a misdetected ranked game keeps its RP.
    do_rp = bool(_RANK_TRACKER) and (ranked is not False or not _RANK_ASSUME_ZERO)

    # ── RP snapshot (starting_rp) ──
    # Grab each player's current RP from the rank tracker cache RIGHT NOW.
    # Because of the EA 2-3 min cache delay, this value is still the
    # BEFORE-match RP. The ending_rp is resolved later by the durable RP queue
    # (_enqueue_rp_pending -> the background sweep) once the EA cache updates.
    if do_rp:
        for p in match["players"]:
            rp_now, _ = _RANK_TRACKER.get_rp(p["name"])
            p["starting_rp"] = rp_now
            p["ending_rp"] = None
            p["rp_change"] = None
    else:
        for p in match["players"]:
            p["starting_rp"] = None
            p["ending_rp"] = None
            p["rp_change"] = None

    append_match(path, match)
    seen.add(fp)
    if sk:
        seen.add(sk)
    status["logged_this_run"] += 1
    status["last_match"] = {
        "session_id": match["session_id"],
        "placed": match["squad_placed"],
        "total_kills": match["total_squad_kills"],
        "ranked": ranked,  # True / False / None(detection off) - shown by front-ends
        "time": datetime.now().strftime("%H:%M:%S"),
        "players": [{"name": p["name"], "kills": p["kills"],
                     "damage": p["damage"]} for p in match["players"]],
    }
    emit("log")
    if log_cb:
        try:
            log_cb(dict(status["last_match"]))
        except Exception:
            pass

    # ── Queue durable RP resolution (handles the EA cache delay) ──
    if do_rp:
        _enqueue_rp_pending(match, path)

    return "logged"


# --------------------------------------------------------------------------- #
# Durable RP resolution
# --------------------------------------------------------------------------- #
# Each logged ranked match snapshots every player's pre-match RP (starting_rp)
# and drops a record in rp_pending.json next to the CSV; a single background
# sweep fills in ending_rp/rp_change once EA's cache propagates the new RP. The
# queue is DURABLE — it survives app restarts and is not bounded by a fixed
# window (the old per-match thread gave up at 300 s and died when the app
# closed). Because the baseline is frozen at match-end, queuing the next game
# immediately can't corrupt the previous match's RP. A record is:
#   {"session_id","name","player_slot","starting_rp","ranked","csv_path",
#    "logged_epoch"}
_RP_PENDING_LOCK = threading.Lock()
_RP_RESOLVER_STARTED = False
_RP_RESOLVER_STOP = threading.Event()


def _rp_pending_path():
    return os.path.join(HERE, "rp_pending.json")


def _load_rp_pending():
    path = _rp_pending_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_rp_pending(records):
    """Atomic write (temp file + os.replace) so a crash/kill mid-write can't
    truncate the queue."""
    path = _rp_pending_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _enqueue_rp_pending(match, csv_path_str):
    """Queue every baseline-bearing player of *match* for durable RP resolution.

    The starting_rp snapshot was taken at log time (frozen pre/at-match RP); the
    background sweep computes the delta once EA propagates. Players with no
    baseline (None — e.g. ALS hadn't resolved them yet) are skipped, same as
    before, but the sweep keeps the rest indefinitely instead of a 300 s window.
    """
    now = time.time()
    new_records = [{
        "session_id": match["session_id"],
        "name": p["name"],
        "player_slot": p["player_slot"],
        "starting_rp": p["starting_rp"],
        "ranked": match.get("ranked"),
        "csv_path": csv_path_str,
        "logged_epoch": now,
    } for p in match["players"] if p.get("starting_rp") is not None]
    if not new_records:
        return
    with _RP_PENDING_LOCK:
        records = _load_rp_pending()
        records.extend(new_records)
        _save_rp_pending(records)
    _ensure_rp_resolver()


def _ensure_rp_resolver():
    """Start the single background resolver thread once (idempotent). No-op when
    no ALS key is configured (_RANK_TRACKER is None)."""
    global _RP_RESOLVER_STARTED
    if _RP_RESOLVER_STARTED or _RANK_TRACKER is None:
        return
    _RP_RESOLVER_STARTED = True
    t = threading.Thread(target=_rp_resolver_loop, daemon=True, name="rp-resolver")
    t.start()


def _rp_resolver_loop():
    try:
        sweep_secs = max(10, int(load_config().get("rp_sweep_seconds", 30)))
    except Exception:
        sweep_secs = 30
    while not _RP_RESOLVER_STOP.is_set():
        try:
            _rp_resolve_sweep()
        except Exception as exc:
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP sweep error - {exc}")
        if _RP_RESOLVER_STOP.wait(sweep_secs):
            break


def _rp_resolve_sweep():
    """One pass over the pending queue: refresh ALS for the players still
    awaiting RP, then resolve each player's EARLIEST pending match in
    chronological order so baseline chaining holds (match B starts where A
    ended). Conservatively flags collapsed multi-game jumps instead of guessing,
    and never caps a positive (a high-kill win can be +500+)."""
    if _RANK_TRACKER is None:
        return
    with _RP_PENDING_LOCK:
        records = _load_rp_pending()
    if not records:
        return
    try:
        cfg = load_config()
    except Exception:
        cfg = {}
    loss_floor = cfg.get("rp_loss_floor", -100)
    expiry_secs = cfg.get("rp_pending_expiry_hours", 24) * 3600
    now = time.time()

    fresh = [r for r in records if (now - r.get("logged_epoch", now)) < expiry_secs]
    for r in records:
        if r in fresh:
            continue
        # Expired: RP never propagated within the window. assume-zero (opt-in)
        # stamps a 0 (floor-protected loss); otherwise leave an honest blank.
        if _RANK_ASSUME_ZERO and r.get("starting_rp") is not None:
            _safe_patch_rp(r.get("csv_path"), r["session_id"],
                           {r["name"]: {"ending_rp": r["starting_rp"], "rp_change": 0}})
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP unresolved (expired) for "
                  f"{r['session_id']} / {r['name']}: recorded 0 (assume-zero on).")
        else:
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP expired unresolved for "
                  f"{r['session_id']} / {r['name']} (left blank).")

    names = sorted({r["name"] for r in fresh})
    if names:
        try:
            _RANK_TRACKER.force_poll_names(names)
        except Exception as exc:
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP poll error - {exc}")

    resolved_keys = set()   # (session_id, name) -> drop from queue
    rebaseline = {}         # (session_id, name) -> corrected starting_rp (chaining)

    by_name = {}
    for r in fresh:
        by_name.setdefault(r["name"], []).append(r)

    for name, recs in by_name.items():
        current, _ = _RANK_TRACKER.get_rp(name)
        if current is None:
            continue  # ALS hasn't resolved this player yet — try next sweep
        recs.sort(key=lambda r: r.get("logged_epoch", 0.0))
        rec = recs[0]   # earliest unresolved match; later ones chain behind it
        start = rec.get("starting_rp")
        if start is None or current == start:
            continue    # no movement yet
        delta = current - start
        key = (rec["session_id"], rec["name"])
        if delta < loss_floor:
            # A single ranked game can't lose this much, so this jump either
            # collapsed two results into one ALS reading (two pending games) or
            # spans an untracked game / cache glitch. Don't fabricate a split —
            # leave this match's RP blank; the match row itself is untouched.
            why = ("spans multiple pending games" if len(recs) >= 2
                   else "exceeds a single-game loss (untracked game or glitch)")
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP ambiguous for "
                  f"{rec['session_id']} / {name}: observed {delta:+d} {why}; "
                  f"left blank (no fabricated split).")
            resolved_keys.add(key)
        else:
            _safe_patch_rp(rec.get("csv_path"), rec["session_id"],
                           {name: {"ending_rp": current, "rp_change": delta}})
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP resolved for "
                  f"{rec['session_id']} / {name}: {delta:+d} (now {current}).")
            resolved_keys.add(key)
        # Chain: the next pending match for this player starts where this one's
        # observed value now sits (whether we attributed it or flagged it).
        if len(recs) >= 2:
            nxt = recs[1]
            rebaseline[(nxt["session_id"], nxt["name"])] = current

    # Reconcile under the lock (a concurrent enqueue may have appended): drop
    # resolved + expired, apply chained baselines, keep everything else.
    with _RP_PENDING_LOCK:
        records = _load_rp_pending()
        out = []
        for r in records:
            key = (r.get("session_id"), r.get("name"))
            if key in resolved_keys:
                continue
            if (now - r.get("logged_epoch", now)) >= expiry_secs:
                continue
            if key in rebaseline:
                r = dict(r)
                r["starting_rp"] = rebaseline[key]
            out.append(r)
        _save_rp_pending(out)


def _safe_patch_rp(csv_path_str, session_id, resolved):
    """Apply a resolved-RP patch to both the CSV and Supabase, isolating each so a
    failure in one (e.g. a CSV rewrite hiccup) is logged but never aborts the
    resolver thread or skips the other sink."""
    try:
        _patch_rp_in_csv(csv_path_str, session_id, resolved)
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] (ALS) CSV RP patch failed for {session_id}: {e}")
    try:
        _patch_rp_in_supabase(session_id, resolved)
    except Exception as e:
        print(f"[{datetime.now():%H:%M:%S}] (ALS) Supabase RP patch failed for {session_id}: {e}")


def _patch_rp_in_csv(csv_path_str, session_id, resolved):
    """Re-read the CSV, update matching rows with ending_rp / rp_change, rewrite.

    *resolved* is ``{name: {"ending_rp": int, "rp_change": int}}``.  Only rows
    whose ``session_id`` matches are touched.
    """
    if not os.path.exists(csv_path_str):
        return
    # Hold the lock across the whole read-modify-rewrite so a concurrent
    # append_match() (or another patch thread) can't clobber the file.
    with _CSV_LOCK:
        # Normalize the header first so an older/ragged file reads cleanly (no None
        # restkey) and writes back against the canonical schema.
        _ensure_canonical_header(csv_path_str)
        with open(csv_path_str, "r", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            rows = list(reader)
        if not fieldnames:
            return  # empty / headerless file, nothing to patch
        changed = False
        for row in rows:
            row.pop(None, None)  # defensive: drop any ragged extra-value restkey
            if row.get("session_id") == session_id and row.get("name") in resolved:
                rp_data = resolved[row["name"]]
                row["ending_rp"] = rp_data["ending_rp"]
                row["rp_change"] = rp_data["rp_change"]
                changed = True
        if not changed:
            return
        # Ensure every canonical column exists in the header even if the file predates it.
        for col in CSV_FIELDS:
            if col not in fieldnames:
                fieldnames.append(col)
        # Atomic rewrite: write a temp file then os.replace() it over the real
        # path, so a crash/kill mid-write (e.g. app closed while a resolver is
        # patching) can never leave a truncated apex_matches.csv.
        tmp = csv_path_str + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, csv_path_str)


def _patch_rp_in_supabase(session_id, resolved):
    """Update Supabase rows with the resolved ending_rp and rp_change.

    *resolved* is ``{name: {"ending_rp": int, "rp_change": int}}``.
    """
    if not _SUPABASE_CLIENT:
        return
    for name, rp_data in resolved.items():
        try:
            _SUPABASE_CLIENT.table("apex_matches").update({
                "ending_rp": rp_data["ending_rp"],
                "rp_change": rp_data["rp_change"],
            }).eq("session_id", session_id).eq("name", name).execute()
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] (Supabase) RP patch error for {name}: {e}")


def run_watch(cfg, forced_res=None, stop_event=None, status_cb=None, log_cb=None):
    """Core watch loop, shared by the console (cmd_watch) and the GUI.

    Logs each new match to the CSV (and Supabase). Drives any front-end through
    callbacks instead of printing: status_cb(status) is called frequently with a
    snapshot dict (state, frame_age, logged_this_run, last_match, ...) carrying an
    'event' key ('start'/'tick'/'heartbeat'/'reacquire'/'announce'/'log'/'stop');
    log_cb(last_match) fires once per newly logged match. Runs until stop_event is
    set. status_cb runs on this thread, so a GUI must marshal back to its own thread
    (don't touch widgets directly). Returns the count logged this run.
    """
    import threading as _th
    stop_event = stop_event or _th.Event()
    sync_roster_to_supabase(cfg)
    # (Re)build the ALS rank tracker from the current config so an API key / roster
    # entered in Settings applies now (Stop->Start) without an app restart, then
    # resume resolving any RP left pending from a previous session (durable queue).
    init_rank_tracker(cfg)
    _ensure_rp_resolver()
    forced = forced_res or cfg.get("force_resolution") or None
    path = csv_path(cfg)
    seen = already_logged_ids(path)
    poll = cfg.get("poll_seconds", 1.0)
    # A frame is only "stale" once it's older than the capture interval plus a
    # margin; otherwise a high throttle_ms (the stutter fix) trips false warnings.
    throttle_s = cfg.get("capture", {}).get("throttle_ms", 1000) / 1000.0
    stale_after = max(5.0, throttle_s + 3.0)
    if cfg.get("low_priority", True):
        set_below_normal_priority()

    status = {"state": "starting", "src": None, "frame_age": None,
              "logged_this_run": 0,
              "already_logged": sum(1 for k in seen if k.startswith("stat:")),
              "last_match": None, "csv_path": path, "resolution": None,
              "profile": None, "match_ranked": False, "event": "start"}

    def emit(event):
        status["event"] = event
        if status_cb:
            try:
                status_cb(dict(status))
            except Exception:
                pass

    emit("start")

    # On-demand mode: keep NO capture session open during gameplay, so an
    # exclusive-fullscreen game keeps its fast present path (no constant stutter).
    # Briefly open capture to probe for the end screen, then close it again.
    # (Not used for OBS mode: reading the virtual camera has zero game overhead,
    # so there is nothing to avoid - the continuous loop below is better there.)
    cap_mode = cfg.get("capture", {}).get("mode", "monitor")
    if cap_mode != "obs" and cfg.get("capture", {}).get("on_demand", False):
        return _watch_on_demand(cfg, forced, path, seen, poll,
                                settle_seconds=cfg.get("settle_seconds", 1.5),
                                hb_secs=cfg.get("heartbeat_seconds", 60),
                                status=status, emit=emit, log_cb=log_cb,
                                stop_event=stop_event)

    dbg = _make_debug_logger(cfg)
    dbg(f"continuous watch started; mode={cap_mode} poll={poll}s csv={path}")
    cap, src, hwnd = open_live_capture(cfg)
    status["src"] = src
    got = cap.wait_first(8)
    status["state"] = "watching" if got else "waiting"
    dbg(f"opened {src}; first frame: {'OK' if got else 'NONE within 8s'}")
    emit("tick")

    last_recheck = time.time()
    hb_secs = cfg.get("heartbeat_seconds", 60)
    last_hb = time.time()
    summary_handled = False
    banner_since = None
    absent_since = None
    announced = False
    banner_logged = False
    settle = cfg.get("settle_seconds", 1.5)
    # Ranked-match detection (default off). During gameplay we throttle-check the
    # top-right HUD badge on frames the loop already grabbed (no extra capture) and
    # latch match_ranked for the match; two consecutive positives are required so a
    # single busy background frame can't false-trigger. The latch is consumed when
    # the summary is logged, then reset for the next match.
    ranked_enabled = cfg.get("ranked_detect_enabled", False)
    ranked_check_secs = cfg.get("ranked_check_seconds", 25)
    match_ranked = False
    ranked_confirm = 0
    last_ranked_check = 0.0
    # Carries the last summary read's fingerprint + capture time for the stability
    # gate, so we only log once the rolling stats have settled (see _try_log_match).
    stable = {"fp": None, "ft": 0.0}
    try:
        while not stop_event.is_set():
            age = cap.last_frame_age()
            status["frame_age"] = age
            status["state"] = "watching" if age < stale_after else "stale"
            # Cheaply re-acquire only when needed. During normal play this is just
            # an IsWindow() check (no expensive window scan), so it won't hitch.
            if time.time() - last_recheck > 30:
                last_recheck = time.time()
                alive = hwnd is not None and _user32.IsWindow(hwnd)
                if not alive or cap.last_frame_age() > 20:
                    cap.release()
                    cap, src, hwnd = open_live_capture(cfg)
                    status["src"] = src
                    dbg(f"re-acquired capture -> {src}")
                    emit("reacquire")
            # Heartbeat tick so a front-end can show it is alive and capturing.
            if hb_secs and time.time() - last_hb >= hb_secs:
                last_hb = time.time()
                emit("heartbeat")
            else:
                emit("tick")

            frame = cap.grab()
            if frame is None:
                stop_event.wait(poll)
                continue
            h, w = frame.shape[:2]
            cfg_eff, prof_key = apply_profile(cfg, w, h, forced)
            scale = scaler(cfg_eff, w, h)
            if not announced:
                announced = True
                status["resolution"] = f"{w}x{h}"
                status["profile"] = prof_key
                emit("announce")
                dbg(f"resolution {w}x{h}, profile={prof_key or '(scaled base)'}")

            # Cheap OCR-free gate every poll. OCR + extraction only happen once per
            # summary, after it has been on screen long enough to finish rendering.
            if not banner_color_present(frame, cfg_eff, scale):
                if absent_since is None:
                    absent_since = time.time()
                # Only arm for a NEW summary once the banner has been gone a few
                # seconds. A brief 1-frame colour flicker on the same summary must not
                # reset summary_handled, or the match gets read and logged twice.
                if time.time() - absent_since >= 3.0:
                    if banner_logged:
                        dbg("banner gone; re-armed for next summary")
                    summary_handled = False
                    banner_since = None
                    banner_logged = False
                    stable["fp"] = None  # fresh stability tracking next summary
                # We're off the summary (gameplay/lobby) — the only time the rank
                # badge is on screen. Throttle-check it and latch on two consecutive
                # hits. Reuses this already-grabbed frame, so it adds no capture.
                if ranked_enabled and not match_ranked and \
                        time.time() - last_ranked_check >= ranked_check_secs:
                    last_ranked_check = time.time()
                    if ranked_badge_present(frame, cfg_eff, scale):
                        ranked_confirm += 1
                        if ranked_confirm >= 2:
                            match_ranked = True
                            status["match_ranked"] = True
                            emit("ranked")  # live "badge grabbed" signal for front-ends
                            dbg("ranked badge confirmed; match flagged RANKED")
                    else:
                        ranked_confirm = 0
                stop_event.wait(poll)
                continue
            absent_since = None
            if banner_since is None:
                banner_since = time.time()
            if not banner_logged:
                banner_logged = True
                dbg("BANNER COLOUR seen; settling before read")
            if summary_handled:
                stop_event.wait(poll)
                continue
            # Let the screen settle so placement/total/stats are fully drawn.
            if time.time() - banner_since < settle:
                stop_event.wait(poll)
                continue
            if not banner_text_present(frame, cfg_eff, scale):
                stop_event.wait(poll)   # banner colour but not a summary banner
                continue

            result = _try_log_match(frame, cfg_eff, scale, path, seen,
                                    status, emit, log_cb,
                                    ranked=(match_ranked if ranked_enabled else None),
                                    stable=stable,
                                    frame_ts=time.time() - cap.last_frame_age())
            if result != "incomplete":
                summary_handled = True  # this summary instance is now processed
                stable["fp"] = None     # done with this summary; reset stability
                # Latch consumed for this match — reset for the next one. (Can't
                # reset in the banner-absent branch above: that runs every gameplay
                # frame and would wipe the detection before the summary is read.)
                match_ranked = False
                ranked_confirm = 0
                last_ranked_check = 0.0
                status["match_ranked"] = False
                dbg(f"read result: {result}"
                    + (f" (now {status['logged_this_run']} logged this run)"
                       if result == "logged" else ""))
            stop_event.wait(poll)
    finally:
        cap.release()
        dbg("continuous watch stopped")
        status["state"] = "stopped"
        emit("stop")
    return status["logged_this_run"]


def _make_debug_logger(cfg):
    """Return a dbg(msg) that appends timestamped lines to debug/tracker_debug.log
    when capture.debug_log is true, else a no-op. Lets you alt-tab out after a
    session and see exactly when the tracker opened capture, what it saw, and when
    it idled - so you can line probes up against any in-game stutter you felt."""
    if not cfg.get("capture", {}).get("debug_log", False):
        return lambda msg: None
    os.makedirs(DEBUG_DIR, exist_ok=True)
    log_path = os.path.join(DEBUG_DIR, "tracker_debug.log")

    def dbg(msg):
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"[{datetime.now():%H:%M:%S.%f}"[:-3] + f"] {msg}\n")
        except Exception:
            pass

    dbg(f"--- session start {datetime.now():%Y-%m-%d %H:%M:%S} ---")
    return dbg


def _watch_on_demand(cfg, forced, path, seen, poll, settle_seconds, hb_secs,
                     status, emit, log_cb, stop_event):
    """Capture only in short bursts so an exclusive-fullscreen game keeps its fast
    present path during gameplay (eliminates the constant WGC capture stutter).

    Idle loop: open capture, grab ONE frame, close it, check for the end-screen
    banner colour, then sleep `idle_probe_seconds`. No session stays open while you
    play. When a banner is spotted, hold that one session open and read at the
    normal poll cadence until the screen settles enough to log (you're on the
    summary by then, so a brief active capture is fine), then close and idle again.
    """
    idle_probe = cfg.get("capture", {}).get("idle_probe_seconds", 8)
    # Open each probe FAST so that once a summary banner is caught and the session
    # is held open, the stability gate's confirming frame arrives sub-second
    # instead of waiting a full throttle_ms. The end screen is static (game idle),
    # so capturing fast there adds no gameplay stutter; a gameplay probe only ever
    # grabs one frame before releasing, so the throttle is irrelevant to it.
    summary_throttle = cfg.get("capture", {}).get("summary_throttle_ms", 250)
    dbg = _make_debug_logger(cfg)
    dbg(f"on-demand watch started; idle_probe={idle_probe}s settle={settle_seconds}s "
        f"poll={poll}s csv={path}")
    announced = False
    last_hb = time.time()
    # Ranked-badge latch (see run_watch). Here the check rides each idle probe
    # frame rather than the continuous poll, but the latch/consume logic matches.
    ranked_enabled = cfg.get("ranked_detect_enabled", False)
    ranked_check_secs = cfg.get("ranked_check_seconds", 25)
    match_ranked = False
    ranked_confirm = 0
    last_ranked_check = 0.0
    stable = {"fp": None, "ft": 0.0}  # stability gate state (see _try_log_match)

    def heartbeat_or_tick():
        nonlocal last_hb
        if hb_secs and time.time() - last_hb >= hb_secs:
            last_hb = time.time()
            emit("heartbeat")
        else:
            emit("tick")

    probe_n = 0
    while not stop_event.is_set():
        probe_n += 1
        t_open = time.time()
        cap, src, hwnd = open_live_capture(cfg, throttle_override=summary_throttle)
        status["src"] = src
        got = cap.wait_first(5)
        frame = cap.grab() if got else None
        open_ms = (time.time() - t_open) * 1000
        if frame is None:
            cap.release()
            status["state"] = "waiting"
            status["frame_age"] = None
            dbg(f"probe #{probe_n}: NO FRAME from {src} after {open_ms:.0f}ms "
                f"(Apex visible?); released, idling {idle_probe}s")
            heartbeat_or_tick()
            stop_event.wait(idle_probe)
            continue

        h, w = frame.shape[:2]
        cfg_eff, prof_key = apply_profile(cfg, w, h, forced)
        scale = scaler(cfg_eff, w, h)
        if not announced:
            announced = True
            status["resolution"] = f"{w}x{h}"
            status["profile"] = prof_key
            emit("announce")
            dbg(f"resolution {w}x{h}, profile={prof_key or '(scaled base)'}")
        status["state"] = "watching"
        status["frame_age"] = cap.last_frame_age()

        if not banner_color_present(frame, cfg_eff, scale):
            # Gameplay probe — check the rank badge on this frame before dropping
            # the session, latching on two consecutive hits (same as run_watch).
            if ranked_enabled and not match_ranked and \
                    time.time() - last_ranked_check >= ranked_check_secs:
                last_ranked_check = time.time()
                if ranked_badge_present(frame, cfg_eff, scale):
                    ranked_confirm += 1
                    if ranked_confirm >= 2:
                        match_ranked = True
                        status["match_ranked"] = True
                        emit("ranked")  # live "badge grabbed" signal for front-ends
                        dbg("ranked badge confirmed; match flagged RANKED")
                else:
                    ranked_confirm = 0
            # No end screen - drop the session immediately. This is the clean,
            # zero-capture gameplay window where the game runs stutter-free.
            cap.release()
            dbg(f"probe #{probe_n}: {src} {w}x{h}, open+grab {open_ms:.0f}ms, "
                f"no banner -> released, idling {idle_probe}s (capture OFF)")
            heartbeat_or_tick()
            stop_event.wait(idle_probe)
            continue

        # Possible end screen: keep THIS session open and read at the normal
        # cadence until it settles and we can log it.
        dbg(f"probe #{probe_n}: BANNER COLOUR seen -> holding session open to read")
        banner_since = time.time()
        stable["fp"] = None  # fresh stability tracking for this summary
        while not stop_event.is_set():
            frame = cap.grab()
            if frame is None:
                stop_event.wait(poll)
                continue
            status["frame_age"] = cap.last_frame_age()
            if not banner_color_present(frame, cfg_eff, scale):
                dbg("  banner gone before read completed; back to idle")
                break  # banner gone before we could read it; back to idle probing
            if time.time() - banner_since < settle_seconds:
                stop_event.wait(poll)
                continue
            if not banner_text_present(frame, cfg_eff, scale):
                dbg("  banner colour but text not a summary; retrying")
                stop_event.wait(poll)   # banner colour but not a summary banner
                continue
            result = _try_log_match(frame, cfg_eff, scale, path, seen,
                                    status, emit, log_cb,
                                    ranked=(match_ranked if ranked_enabled else None),
                                    stable=stable,
                                    frame_ts=time.time() - cap.last_frame_age())
            dbg(f"  read result: {result}"
                + (f" (now {status['logged_this_run']} logged this run)"
                   if result == "logged" else ""))
            if result != "incomplete":
                stable["fp"] = None     # done with this summary; reset stability
                # Latch consumed for this match; reset for the next one.
                match_ranked = False
                ranked_confirm = 0
                last_ranked_check = 0.0
                status["match_ranked"] = False
                break   # logged or already-seen; stop reading this summary
            stop_event.wait(poll)     # still animating in; retry
        cap.release()
        dbg(f"probe #{probe_n}: released after summary read, idling {idle_probe}s (capture OFF)")
        # Idle again. If we just logged, this also waits out part of the summary
        # so the next probe doesn't immediately re-read the same (now deduped)
        # screen more than necessary.
        stop_event.wait(idle_probe)

    dbg("on-demand watch stopped")
    status["state"] = "stopped"
    emit("stop")
    return status["logged_this_run"]


def cmd_watch(forced_res=None):
    """Console front-end for run_watch: prints the start banner, heartbeats, and
    each logged match."""
    cfg = load_config()
    path = csv_path(cfg)
    seen_n = sum(1 for k in already_logged_ids(path) if k.startswith("stat:"))
    print(f"Watching. {seen_n} matches already logged. Ctrl+C to stop.\nCSV: {path}")

    def status_cb(s):
        ev = s.get("event")
        if ev == "announce":
            if s["profile"]:
                print(f"Using calibration profile for {s['profile']}.")
            else:
                print(f"Resolution {s['resolution']}: scaling base regions to fit "
                      f"(no profile for this resolution).")
        elif ev == "reacquire":
            print(f"[{datetime.now():%H:%M:%S}] re-acquired {s['src']}")
        elif ev == "ranked":
            print(f"[{datetime.now():%H:%M:%S}] RANKED badge detected - this match "
                  f"will be tagged ranked.")
        elif ev == "heartbeat":
            age = s.get("frame_age") or 0
            health = (f"capture OK ({age:.0f}s old frame)" if s["state"] == "watching"
                      else f"capture STALE ({age:.0f}s) - is Apex visible?")
            print(f"[{datetime.now():%H:%M:%S}] still watching {s['src']} | {health} | "
                  f"{s['logged_this_run']} logged this run")

    def log_cb(m):
        names = ", ".join(f"{p['name']}({p['kills']}k/{p['damage']}dmg)" for p in m["players"])
        # Show how the match was tagged: RANKED / PUB, or nothing if detection is off.
        tag = {True: " [RANKED]", False: " [PUB]"}.get(m.get("ranked"), "")
        print(f"[{datetime.now():%H:%M:%S}] logged {m['session_id']}{tag}  "
              f"#{m['placed']} {m['total_kills']}k -> {names}")

    stop_event = threading.Event()
    try:
        run_watch(cfg, forced_res, stop_event=stop_event, status_cb=status_cb, log_cb=log_cb)
    except KeyboardInterrupt:
        stop_event.set()
        print("\nStopped.")


def cmd_setup():
    """Interactive: ask the friend what resolution they run Apex at and save it to
    config.json, so the matching calibration profile (if any) is used. Run once."""
    cfg = load_config()
    base = f"{cfg.get('base_width', 1920)}x{cfg.get('base_height', 1080)}"
    profiles = cfg.get("profiles") or {}
    current = cfg.get("force_resolution") or "(auto-detect)"
    print("Apex Tracker - resolution setup")
    print(f"Current setting: {current}")
    if profiles:
        print(f"Calibration profiles available: {', '.join(sorted(profiles))}")
    print("\nWhat resolution do you run Apex at in-game?")
    print("  Common: 1920x1080, 2560x1440, 3440x1440 (ultrawide), 3840x2160")
    print("  (Leave blank to auto-detect from the screen on every run.)")
    try:
        # lstrip the BOM in case input was piped from a UTF-16/BOM source.
        ans = input("Resolution: ").strip().lstrip(chr(0xFEFF)).strip().lower().replace(" ", "")
    except EOFError:
        print("No input received; leaving the setting unchanged.")
        return
    if ans == "":
        cfg.pop("force_resolution", None)
        save_config(cfg)
        print("Set to auto-detect. Done.")
        return
    if not re.match(r"^\d{3,5}x\d{3,5}$", ans):
        print(f"'{ans}' is not WIDTHxHEIGHT (e.g. 2560x1440). Nothing changed.")
        return
    cfg["force_resolution"] = ans
    save_config(cfg)
    if ans in profiles:
        print(f"Saved {ans}. A calibration profile exists for it and will be used.")
    elif ans == base:
        print(f"Saved {ans}. That is the base resolution; regions are used as-is.")
    else:
        print(f"Saved {ans}. No exact calibration profile yet - the {base} regions "
              f"will be auto-scaled to fit. If the numbers look off, see CALIBRATION.md.")


def main():
    # OCR'd gamertags can contain non-Latin glyphs the default Windows console
    # (cp1252) can't encode, which would crash any print() of a name (batch,
    # calibrate, and the live watcher's per-match line). Degrade unencodable
    # chars instead of raising. Best-effort: a redirected/None stdout just skips.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = sys.argv[1:]
    # Pull out an optional "--res WIDTHxHEIGHT" override from anywhere in the args.
    forced_res = None
    if "--res" in args:
        i = args.index("--res")
        if i + 1 < len(args):
            forced_res = args[i + 1]
            del args[i:i + 2]
        else:
            del args[i]
    cmd = args[0] if args else "watch"
    pos = args[1] if len(args) > 1 else None
    if cmd == "monitors":
        cmd_monitors()
    elif cmd == "devices":
        cmd_devices()
    elif cmd == "batch":
        cmd_batch(pos, forced_res)
    elif cmd == "shot":
        cmd_shot(pos)   # optional mode override, e.g. "shot obs"
    elif cmd == "calibrate":
        cmd_calibrate(pos, forced_res)
    elif cmd == "setup":
        cmd_setup()
    elif cmd == "watch":
        cmd_watch(forced_res)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
