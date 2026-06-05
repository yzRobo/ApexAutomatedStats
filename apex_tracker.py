"""
Apex Legends post-game summary tracker.

Passive, read-only screen reader: it captures the screen (same kind of OS
capture OBS uses), detects the gold CHAMPIONS / SUMMARY screen, OCR-reads each
player card + the match/session id, and appends rows to a CSV. It never touches
the Apex process, never reads game memory, and never sends input to the game.

Usage:
    py apex_tracker.py shot                 # save one capture to debug/ (prove capture works)
    py apex_tracker.py monitors             # list detected monitors
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
from datetime import datetime
import traceback

try:
    from dotenv import load_dotenv
    from supabase import create_client, Client
except ImportError:
    load_dotenv = None
    create_client = None

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

__version__ = "1.2.1"
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
    return any(k in text for k in d.get("match_texts", ["CHAMPION", "ELIMINAT", "SQUAD"]))


def is_summary_screen(frame, cfg, scale):
    """Full check (color gate + OCR confirm). Used by calibrate / batch."""
    return banner_color_present(frame, cfg, scale) and banner_text_present(frame, cfg, scale)


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
        damage = parse_int(ocr_field(field("damage", value_w), numeric=True))
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
]


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


def append_match(path, match):
    new_file = not os.path.exists(path)
    ts = datetime.now().isoformat(timespec="seconds")
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if new_file:
            w.writeheader()
            
        supabase_rows = []
        for p in match["players"]:
            row_data = {
                "timestamp": ts,
                "session_id": match["session_id"],
                "squad_placed": match["squad_placed"],
                "total_squad_kills": match["total_squad_kills"],
                **p,
            }
            w.writerow(row_data)
            supabase_rows.append(row_data)
            
        if _SUPABASE_CLIENT:
            try:
                # returning="minimal" avoids reading the rows back, so this works
                # under an insert-only RLS policy (no SELECT granted to anon).
                _SUPABASE_CLIENT.table("apex_matches").insert(
                    supabase_rows, returning="minimal"
                ).execute()
                print(f"[{datetime.now():%H:%M:%S}] (Supabase) synced {len(supabase_rows)} player records.")
            except Exception as e:
                print(f"[{datetime.now():%H:%M:%S}] (Supabase) Error syncing to Supabase: {e}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def open_live_capture(cfg):
    """Open the live capture.

    mode "monitor" (default): capture the whole monitor Apex is on. This is the
    smooth path for exclusive-fullscreen games (what OBS display capture uses).
    mode "window": capture just the Apex window (works on any monitor but can make
    a fullscreen game stutter).

    Returns (capture, description, hwnd). hwnd is the tracked Apex window (or None).
    """
    cap_cfg = cfg.get("capture", {})
    exe_names = cap_cfg.get("exe_names", ["r5apex_dx12.exe", "r5apex.exe"])
    throttle = cap_cfg.get("throttle_ms", 500)
    mode = cap_cfg.get("mode", "monitor")
    hwnd = find_window_hwnd(exe_names)

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


def cmd_shot():
    cfg = load_config()
    os.makedirs(DEBUG_DIR, exist_ok=True)
    cap, src, _ = open_live_capture(cfg)
    print(f"Capturing {src} ...")
    if not cap.wait_first(8):
        print("No frame received. Is Apex running and visible?")
        cap.release()
        return
    frame = cap.grab()
    out = os.path.join(DEBUG_DIR, "capture_live.png")
    cv2.imwrite(out, frame)
    black = float(frame.mean()) < 2.0
    print(f"saved {out}  {frame.shape[1]}x{frame.shape[0]}  mean_brightness={frame.mean():.1f}"
          + ("  <-- LOOKS BLACK (capture blocked)" if black else "  (looks good)"))
    cap.release()


def draw_boxes(frame, cfg, scale):
    out = frame.copy()
    def box(b, color, label):
        x, y, w, h = scale(b)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, 2)
        cv2.putText(out, label, (x, max(0, y - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    box(cfg["detect"]["banner"], (0, 255, 255), "banner")
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
    forced = forced_res or cfg.get("force_resolution") or None
    path = csv_path(cfg)
    seen = already_logged_ids(path)
    poll = cfg.get("poll_seconds", 1.0)
    if cfg.get("low_priority", True):
        set_below_normal_priority()

    status = {"state": "starting", "src": None, "frame_age": None,
              "logged_this_run": 0,
              "already_logged": sum(1 for k in seen if k.startswith("stat:")),
              "last_match": None, "csv_path": path, "resolution": None,
              "profile": None, "event": "start"}

    def emit(event):
        status["event"] = event
        if status_cb:
            try:
                status_cb(dict(status))
            except Exception:
                pass

    emit("start")
    cap, src, hwnd = open_live_capture(cfg)
    status["src"] = src
    got = cap.wait_first(8)
    status["state"] = "watching" if got else "waiting"
    emit("tick")

    last_recheck = time.time()
    hb_secs = cfg.get("heartbeat_seconds", 60)
    last_hb = time.time()
    logged_this_run = 0
    summary_handled = False
    banner_since = None
    absent_since = None
    announced = False
    settle = cfg.get("settle_seconds", 1.5)
    try:
        while not stop_event.is_set():
            age = cap.last_frame_age()
            status["frame_age"] = age
            status["state"] = "watching" if age < 5 else "stale"
            # Cheaply re-acquire only when needed. During normal play this is just
            # an IsWindow() check (no expensive window scan), so it won't hitch.
            if time.time() - last_recheck > 30:
                last_recheck = time.time()
                alive = hwnd is not None and _user32.IsWindow(hwnd)
                if not alive or cap.last_frame_age() > 20:
                    cap.release()
                    cap, src, hwnd = open_live_capture(cfg)
                    status["src"] = src
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

            # Cheap OCR-free gate every poll. OCR + extraction only happen once per
            # summary, after it has been on screen long enough to finish rendering.
            if not banner_color_present(frame, cfg_eff, scale):
                if absent_since is None:
                    absent_since = time.time()
                # Only arm for a NEW summary once the banner has been gone a few
                # seconds. A brief 1-frame colour flicker on the same summary must not
                # reset summary_handled, or the match gets read and logged twice.
                if time.time() - absent_since >= 3.0:
                    summary_handled = False
                    banner_since = None
                stop_event.wait(poll)
                continue
            absent_since = None
            if banner_since is None:
                banner_since = time.time()
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

            match = extract_match(frame, cfg_eff, scale)
            # Wait until every player is named AND placement has rendered, so we
            # never log a half-drawn screen (the cause of the earlier blank rows).
            if any(not p["name"] for p in match["players"]) or match["squad_placed"] is None:
                stop_event.wait(poll)   # still animating in; retry next poll
                continue
            summary_handled = True  # this summary instance is now processed
            if not match["session_id"]:
                match["session_id"] = match_key(match)  # persist so restarts dedup
            # Dedup on the stable stats, not the flaky session id. Skip if this match
            # was already logged - even if its id OCRs differently this time.
            sk = sid_norm(match["session_id"])
            fp = stat_fingerprint(match["players"], match["squad_placed"],
                                  match["total_squad_kills"])
            if fp in seen or (sk and sk in seen):
                stop_event.wait(poll)
                continue
            append_match(path, match)
            seen.add(fp)
            if sk:
                seen.add(sk)
            logged_this_run += 1
            status["logged_this_run"] = logged_this_run
            status["last_match"] = {
                "session_id": match["session_id"],
                "placed": match["squad_placed"],
                "total_kills": match["total_squad_kills"],
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
            stop_event.wait(poll)
    finally:
        cap.release()
        status["state"] = "stopped"
        emit("stop")
    return logged_this_run


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
        elif ev == "heartbeat":
            age = s.get("frame_age") or 0
            health = (f"capture OK ({age:.0f}s old frame)" if s["state"] == "watching"
                      else f"capture STALE ({age:.0f}s) - is Apex visible?")
            print(f"[{datetime.now():%H:%M:%S}] still watching {s['src']} | {health} | "
                  f"{s['logged_this_run']} logged this run")

    def log_cb(m):
        names = ", ".join(f"{p['name']}({p['kills']}k/{p['damage']}dmg)" for p in m["players"])
        print(f"[{datetime.now():%H:%M:%S}] logged {m['session_id']}  "
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
    elif cmd == "batch":
        cmd_batch(pos, forced_res)
    elif cmd == "shot":
        cmd_shot()
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
