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
from rp_resolver import resolve_rp

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

__version__ = "1.6.8"
REPO = "yzRobo/ApexAutomatedStats"  # for the in-app update check


def latest_release_version(timeout=4):
    """Return the latest GitHub release tag (e.g. 'v1.2.0'), or None on failure.
    Stdlib only; used by the GUI's update check. Never raises."""
    return latest_release_info(timeout)[0]


def latest_release_info(timeout=6):
    """Return ``(tag, zip_url)`` for the latest GitHub release, or ``(None, None)``.
    ``zip_url`` is the ApexTracker_share.zip asset's download URL. Stdlib only;
    never raises. Used by the GUI's update check + one-click self-updater."""
    import urllib.request
    url = f"https://api.github.com/repos/{REPO}/releases/latest"
    try:
        req = urllib.request.Request(
            url, headers={"Accept": "application/vnd.github+json",
                          "User-Agent": "apex-tracker"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        zip_url = None
        for asset in data.get("assets") or []:
            if (asset.get("name") or "").lower().endswith(".zip"):
                zip_url = asset.get("browser_download_url")
                break
        return data.get("tag_name"), zip_url
    except Exception:
        return None, None


def _version_tuple(v):
    nums = re.findall(r"\d+", v or "")[:3]
    return tuple(int(n) for n in nums) + (0,) * (3 - len(nums))


def update_available(latest_tag):
    """True if *latest_tag* (e.g. 'v1.5.2') is a newer version than this build."""
    return bool(latest_tag) and _version_tuple(latest_tag) > _version_tuple(__version__)


# --------------------------------------------------------------------------- #
# One-click self-update (frozen build only)
# --------------------------------------------------------------------------- #
# The packaged app is a one-folder build (the two exes + a ~238 MB _internal/).
# To update without the user manually replacing files, we download the new
# release zip, then hand off to a tiny batch helper that — once THIS process has
# exited so the files unlock — swaps in the new exes + _internal and relaunches.
# User files (config.json, .env, the CSV, rp_pending.json) are NEVER touched: the
# helper only replaces _internal + the two exes. The swap is move-based so a
# failed copy leaves the working install intact.
_UPDATE_BAT = r'''@echo off
setlocal enableextensions
set "SRC={src}"
set "DST={dst}"
set "LOG=%DST%\_update_log.txt"
>"%LOG%" echo [start] %date% %time%
REM The app exits right after launching us; a fixed wait lets the running exe and
REM its _internal DLLs unlock before we swap. (A PID-wait pipe proved unreliable
REM when detached, so we use a simple timed wait instead.)
ping -n 8 127.0.0.1 >nul
>>"%LOG%" echo [copy _internal]
robocopy "%SRC%\_internal" "%DST%\_internal_new" /E /R:4 /W:2 /NFL /NDL /NJH /NJS /NP >>"%LOG%" 2>&1
if errorlevel 8 ( >>"%LOG%" echo [robocopy FAILED ec=%errorlevel%] & goto fail )
>>"%LOG%" echo [swap files]
if exist "%DST%\_internal_old" rmdir /s /q "%DST%\_internal_old"
move "%DST%\_internal" "%DST%\_internal_old" >>"%LOG%" 2>&1
move "%DST%\_internal_new" "%DST%\_internal" >>"%LOG%" 2>&1
copy /y "%SRC%\ApexTracker.exe" "%DST%\ApexTracker.exe" >>"%LOG%" 2>&1
copy /y "%SRC%\ApexTrackerUI.exe" "%DST%\ApexTrackerUI.exe" >>"%LOG%" 2>&1
rmdir /s /q "%DST%\_internal_old" 2>nul
rmdir /s /q "%DST%\_update_staging" 2>nul
>>"%LOG%" echo [done; relaunching]
start "" "%DST%\{ui}"
del "%~f0"
exit /b 0
:fail
>>"%LOG%" echo [restore + relaunch existing version]
if exist "%DST%\_internal_new" rmdir /s /q "%DST%\_internal_new"
if not exist "%DST%\_internal" if exist "%DST%\_internal_old" move "%DST%\_internal_old" "%DST%\_internal" >nul
start "" "%DST%\{ui}"
del "%~f0"
exit /b 1
'''


def download_update(zip_url, progress_cb=None, timeout=120):
    """Download the release zip and extract it to a staging folder next to the exe.
    Returns the staged ``ApexTracker`` dir. Raises on failure. ``progress_cb`` is
    called with a 0.0–1.0 fraction as bytes arrive (best-effort)."""
    import urllib.request
    import zipfile
    import shutil
    staging_root = os.path.join(HERE, "_update_staging")
    shutil.rmtree(staging_root, ignore_errors=True)
    os.makedirs(staging_root, exist_ok=True)
    zip_path = os.path.join(staging_root, "update.zip")
    req = urllib.request.Request(zip_url, headers={"User-Agent": "apex-tracker"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        done = 0
        with open(zip_path, "wb") as f:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if progress_cb and total:
                    try:
                        progress_cb(done / total)
                    except Exception:
                        pass
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(staging_root)
    try:
        os.remove(zip_path)
    except OSError:
        pass
    inner = os.path.join(staging_root, "ApexTracker")
    return inner if os.path.isdir(inner) else staging_root


def apply_update_and_restart(staging_dir):
    """Write + launch the helper batch that swaps in *staging_dir* after this
    process exits, then relaunches the UI. Frozen build only. Returns True if the
    helper was launched (the caller MUST then exit the process); False otherwise."""
    import subprocess
    if not getattr(sys, "frozen", False) or not os.path.isdir(staging_dir):
        return False
    bat = os.path.join(HERE, "_apply_update.bat")
    script = _UPDATE_BAT.format(src=staging_dir, dst=HERE, ui="ApexTrackerUI.exe")
    with open(bat, "w", encoding="ascii", errors="replace", newline="\r\n") as f:
        f.write(script)
        f.flush()
        os.fsync(f.fileno())
    # Launch detached with explicit DEVNULL handles (a --noconsole app has no valid
    # std handles to inherit) and a new process group, so the helper survives our
    # exit and runs cleanly.
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(["cmd", "/c", bat],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL,
                     creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
                     close_fds=True, cwd=HERE)
    return True

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
# Why RP tracking is off when _RANK_TRACKER is None, for the status display. With
# no key every match logs NULL RP; that used to be completely silent (a squadmate's
# PC ran for days like that), so it is now surfaced in the GUI and the debug log.
_RP_OFF_REASON = "no ALS API key set"

# Diagnostics sink for code that runs outside the watch loops (RP sweep, Supabase
# writes). The GUI build sends stdout to os.devnull, so print() alone vanished; a
# watch run points this at its debug logger as well.
_DIAG = None


def _note(msg):
    """print() plus the debug log (when a watch run has enabled it)."""
    try:
        print(f"[{datetime.now():%H:%M:%S}] {msg}")
    except Exception:
        pass  # a broken stdout must never turn a diagnostic into a failure
    if _DIAG:
        try:
            _DIAG(msg)
        except Exception:
            pass


def _rp_state():
    """``(state, detail)`` of RP tracking: 'off' / 'starting' / 'ok' / 'failing'."""
    if _RANK_TRACKER is None:
        return "off", _RP_OFF_REASON
    try:
        return _RANK_TRACKER.health()
    except Exception as e:
        return "failing", str(e)
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
    global _RANK_TRACKER, _RANK_TRACKER_SIG, _RP_OFF_REASON
    key = _resolve_als_key(cfg)
    if not key:
        if _RANK_TRACKER is not None:
            try:
                _RANK_TRACKER.stop()
            except Exception:
                pass
        _RANK_TRACKER, _RANK_TRACKER_SIG = None, None
        _RP_OFF_REASON = "no ALS API key set (Settings)"
        return
    names = list(cfg.get("known_names", []))
    uids = dict(cfg.get("als_uids", {}) or {})
    poll = cfg.get("rank_poll_seconds", 30)
    # Migrate the pre-v1.6 default: 120s is too coarse for RP (results post in
    # bursts, so back-to-back games collapse into one reading). 30s is the v2
    # default. Only the exact old default is bumped, so an intentional custom
    # value is respected. Configs preserved across updates never got this.
    if poll == 120:
        poll = 30
        print("(ALS) migrated rank_poll_seconds 120 -> 30 (finer RP; "
              "open Settings and Save to make it permanent).")
    sig = (key, tuple(names), tuple(sorted(uids.items())), poll)
    if _RANK_TRACKER is not None and sig == _RANK_TRACKER_SIG:
        return  # nothing relevant changed
    if _RANK_TRACKER is not None:
        try:
            _RANK_TRACKER.stop()
        except Exception:
            pass
    try:
        # The always-on RP ledger lives next to rp_pending.json (writable app dir,
        # not the PyInstaller bundle). §4.1.
        _RANK_TRACKER = RankTracker(
            api_key=key, known_names=names, poll_seconds=poll, uids=uids,
            on_poll=_sync_ranks_to_supabase,
            ledger_path=os.path.join(HERE, "rp_ledger.jsonl"),
            retention_days=cfg.get("rp_ledger_retention_days", 14),
            reset_drop=cfg.get("rp_reset_drop", 800))
        _RANK_TRACKER_SIG = sig
        print(f"(ALS) Rank tracker started for {len(names)} players "
              f"(poll every {poll}s).")
    except Exception as e:
        _RANK_TRACKER, _RANK_TRACKER_SIG = None, None
        _RP_OFF_REASON = f"rank tracker failed to start: {e}"
        print(f"Warning: Failed to start rank tracker: {e}")

# Keep OCR from grabbing every CPU core (which would hitch the game when it runs).
os.environ.setdefault("OMP_NUM_THREADS", "2")


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config():
    # utf-8-sig tolerates a BOM, which some editors / PowerShell add when saving.
    with open(os.path.join(HERE, "config.json"), "r", encoding="utf-8-sig") as f:
        return _migrate_config(json.load(f))


def _migrate_config(cfg):
    """In-place fixes for shipped values found wrong after release. The updater keeps
    each install's config.json, so a corrected default only reaches existing installs
    this way. Each fix applies only while the exact shipped value is still present, so
    a hand-calibrated region is never touched; a later Settings save persists it."""
    rows = ((cfg.get("profiles") or {}).get("1920x1080-native-16x9") or {}).get("rows") or {}
    # The native-16:9 Revive/Respawn boxes sat too low and cut the top off the digits
    # (respawn lost ~9 of 21 px), so that profile logged revive 1 as 7/None and
    # respawn 0 as 6. Glyph rows measured on both native-16:9 frames: 624-646 / 694-716.
    if rows.get("revive") == {"y": 628, "h": 34} and rows.get("respawn") == {"y": 704, "h": 34}:
        rows["revive"] = {"y": 618, "h": 34}
        rows["respawn"] = {"y": 688, "h": 34}
    return cfg


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


# The recognizer sometimes returns lone digits in their full-width form ('２'), which
# pass str.isdigit() but are stripped by parse_int's ASCII filter, so a correct vote
# became None. Folded only inside the single-digit vote, not in digitize(): there a
# full-width digit beside CJK junk ('冏７' for a lone gold '1') must stay unreadable
# rather than turn into a plausible wrong number.
_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


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
                digits = [c for c in digitize(_rec(tiled, up)).translate(_FULLWIDTH_DIGITS)
                          if c in "0123456789"]
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


def _gold_mask(img_bgr):
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, np.array([15, 80, 90]), np.array([42, 255, 255]))


def ocr_gold_number(img_bgr, upscale=4):
    """Header numbers (#placed, total kills) are gold on a busy bar next to white
    labels. Isolate the gold pixels so only the number remains, then read it."""
    if img_bgr is None or img_bgr.size == 0:
        return ""
    iso = cv2.bitwise_and(img_bgr, img_bgr, mask=_gold_mask(img_bgr))
    return digitize(_rec(iso, upscale))


# The header is right-aligned: "#NN" sits a fixed distance left of the total-kills
# number, so it moves ~24 base px left when total kills has two digits. With a fixed
# placement box the leading digit of #10-#20 then falls outside it: #10 / #20 read as
# 0 (failing the plausibility gate on every retry, so the match was never logged,
# 2026-08-09) and #11-#19 read as a plausible, wrong 1-9. So the box is anchored to
# where the total-kills ink starts. _PLACED_INK_REF is that ink's left edge, relative
# to header.total_kills.x, for the single wide digit the box was calibrated on.
_PLACED_INK_REF = 89    # base px
_PLACED_SHIFT = (-40, 20)  # clamp, so a stray speck can't drag the box far
_PLACED_MIN_SHIFT = 8   # smaller offsets (a single-digit total) keep the configured box


def _gold_ink_left(img_bgr, min_run):
    """x of the first run of >= ``min_run`` columns holding gold ink, or None."""
    if img_bgr is None or img_bgr.size == 0:
        return None
    cols = (_gold_mask(img_bgr) > 0).sum(axis=0) >= max(3, img_bgr.shape[0] // 10)
    run = 0
    for x, ink in enumerate(cols):
        run = run + 1 if ink else 0
        if run >= min_run:
            return x - run + 1
    return None


def read_squad_placed(frame, cfg, scale):
    """Squad placement from a box anchored to the total-kills number (see
    _PLACED_INK_REF). Falls back to the configured box when the anchor isn't found,
    the shift is small, or the anchored read isn't a placement."""
    header = cfg["header"]
    box = header["squad_placed"]
    narrow = parse_int(ocr_gold_number(crop(frame, scale(box))))
    sx = frame.shape[1] / cfg["base_width"]
    k = cfg["base_width"] / 1920  # the constants are in 1920-wide base px
    tk = scale(header["total_kills"])
    left = _gold_ink_left(crop(frame, tk), max(2, int(round(4 * sx))))
    if left is None:
        return narrow
    dx = (tk[0] + left) / sx - (header["total_kills"]["x"] + _PLACED_INK_REF * k)
    dx = int(round(min(max(dx, _PLACED_SHIFT[0] * k), _PLACED_SHIFT[1] * k)))
    if abs(dx) < _PLACED_MIN_SHIFT * k:
        return narrow
    anchored = parse_int(ocr_gold_number(crop(frame, scale(dict(box, x=box["x"] + dx)))))
    return anchored if anchored is not None and 1 <= anchored <= 20 else narrow


# The character art sits to the right of each card's stat column and reaches into
# the value boxes; a bright vertical edge of it OCRs as a phantom TRAILING digit
# that the white-pixel mask below can't remove when the art itself is bright
# (TCremer22 damage 768 -> 7687 and knocks 1 -> 11 on the same card, 2026-10-24).
# The values are left-aligned with at most ~9px between glyphs at 1080p, while the
# art starts >=44px past the text (measured on 7 real summaries), so cutting at the
# first 20px empty run drops the art and never a real glyph.
_ART_GAP = 20  # base-res px


def trim_trailing_art(img_bgr, gap_px):
    """Cut a left-aligned value crop at the first run of >= ``gap_px`` empty
    columns after its text starts, so card art further right never reaches the
    OCR. Returns the crop unchanged when there is no such gap (the old behaviour),
    so the worst case is no change, never a clipped digit."""
    if img_bgr is None or img_bgr.size == 0 or gap_px <= 0:
        return img_bgr
    ink = (img_bgr.max(axis=2) > 140).sum(axis=0) >= 2  # text or art in the column
    xs = np.flatnonzero(ink)
    if not xs.size:
        return img_bgr
    run = 0
    for x in range(int(xs[0]), len(ink)):
        run = 0 if ink[x] else run + 1
        if run >= gap_px:
            # Keep a few px of the gap so the last glyph's anti-aliased edge survives.
            return img_bgr[:, :x - run + 1 + max(1, gap_px // 5)]
    return img_bgr


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
    """'5 / 3 / 5' -> (5, 3, 5). Tolerates OCR noise around the slashes.

    The card's italic slashes can OCR as the digit 1, fusing the whole line into
    one run ('7 / 7 / 7' -> '71717', seen live 2026-10-25). That read failed the
    plausibility gate on every retry, so the match was silently never logged.
    When fewer than three numbers come back, re-split the runs at their 1s - but
    only when exactly ONE split gives three believable stats. Anything ambiguous
    stays unread (None) so the caller retries instead of logging a guess."""
    groups = re.findall(r"\d+", text or "")
    if len(groups) < 3:
        fused = _split_fused_kak(groups)
        if fused:
            return fused
    nums = [int(n) for n in groups[:3]]
    while len(nums) < 3:
        nums.append(None)
    return nums[0], nums[1], nums[2]


def _split_fused_kak(groups):
    """The unique (kills, assists, knocks) reading of the digit runs ``groups``
    with some '1's taken as slashes, or None when there is none or several
    (e.g. '111111' could be 11/1/1, 1/11/1 or 1/1/11)."""
    def stat(s):
        return len(s) <= 2 and (s == "0" or s[0] != "0") and int(s) <= _MAX_COUNT

    def splits(run, parts):
        # Every way to cut `run` into `parts` stats separated by single '1's.
        if parts == 1:
            return [(int(run),)] if stat(run) else []
        out = []
        for i in range(1, len(run) - 1):
            if run[i] == "1" and stat(run[:i]):
                out += [(int(run[:i]),) + rest for rest in splits(run[i + 1:], parts - 1)]
        return out

    def combos(gs, parts):
        # Spread `parts` stats over the runs, each run yielding at least one.
        if not gs:
            return [()] if parts == 0 else []
        out = []
        for k in range(1, parts - len(gs) + 2):
            for head in splits(gs[0], k):
                out += [head + rest for rest in combos(gs[1:], parts - k)]
        return out

    found = set(combos(groups, 3))
    return found.pop() if len(found) == 1 else None


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


def _implausible_detail(match):
    """Which values failed _stats_plausible, e.g. 'placement 0, slot 1 kills 71717'."""
    out = []
    sp = match.get("squad_placed")
    if sp is not None and not (1 <= sp <= 20):
        out.append(f"placement {sp}")
    for p in match["players"]:
        if (p.get("damage") or 0) > _MAX_DAMAGE:
            out.append(f"slot {p['player_slot']} damage {p['damage']}")
        for k in ("kills", "assists", "knocks", "revive_given", "respawn_given"):
            if (p.get(k) or 0) > _MAX_COUNT:
                out.append(f"slot {p['player_slot']} {k} {p[k]}")
    return ", ".join(out) or "unknown"


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

    def grab_with_ts(self):
        """``(frame, capture_time)`` read together. Deriving the time afterwards
        from last_frame_age() let a frame that arrived during the banner OCR label
        the OLDER frame as new, so the stability gate could "confirm" a read with
        a second read of the same frame."""
        with self._lock:
            if self._latest is None:
                return None, None
            return self._latest.copy(), self._last_ts

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
    name_hint (default the OBS Virtual Camera), or None.

    Runs pygrabber's DirectShow enumeration in a DEDICATED short-lived thread that
    does its own CoInitialize/CoUninitialize. This is required, not cosmetic: the
    enumeration is COM (DirectShow), and in a long-lived process with other threads
    touching COM (the GUI: tkinter main thread + the ALS rank-tracker daemon + the
    watch worker thread), calling it on those threads intermittently returns None
    even though the camera is present — which surfaced as "OBS Virtual Camera NOT
    FOUND" in OBS mode. Isolating each call in a fresh STA apartment makes it
    reliable regardless of what the rest of the process did with COM."""
    hint = name_hint.lower()
    result = [None]

    def _enum():
        inited = False
        try:
            import comtypes
            comtypes.CoInitialize()
            inited = True
        except Exception:
            pass
        try:
            from pygrabber.dshow_graph import FilterGraph
            names = FilterGraph().get_input_devices()
            for i, n in enumerate(names):
                if hint in (n or "").lower():
                    result[0] = i
                    break
        except Exception:
            pass
        finally:
            if inited:
                try:
                    import comtypes
                    comtypes.CoUninitialize()
                except Exception:
                    pass

    th = threading.Thread(target=_enum, daemon=True)
    th.start()
    th.join(timeout=8)
    return result[0]


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

    def grab_with_ts(self):
        """``(frame, capture_time)`` read together (see WGCCapture.grab_with_ts)."""
        with self._lock:
            if self._latest is None:
                return None, None
            return self._latest.copy(), self._last_ts

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
    squad_placed = read_squad_placed(frame, cfg, scale)
    total_kills = parse_int(ocr_gold_number(crop(frame, scale(header["total_kills"]))))

    rows = cfg["rows"]
    value_w = cfg.get("value_width", 150)
    single_w = cfg.get("single_width", 75)
    art_gap = scale({"x": 0, "y": 0, "w": _ART_GAP, "h": 0})[2]
    players = []
    for slot, col in enumerate(cfg["columns"], start=1):
        def field(row_key, width):
            box = {"x": col["x"], "y": rows[row_key]["y"], "w": width, "h": rows[row_key]["h"]}
            return crop(frame, scale(box))

        name = snap_name(ocr_field(field("name", col["w"])), cfg)
        kills, assists, knocks = parse_kak(ocr_field(
            trim_trailing_art(field("kak", value_w), art_gap), numeric=True))
        damage = parse_int(ocr_white_number(
            trim_trailing_art(field("damage", value_w), art_gap)))
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

    # A lone gold '1' doesn't OCR (the recognizer returns CJK / punctuation), so every
    # 1-kill game logged total_squad_kills NULL (83 of 87 by 2026-10). Where the total
    # was read it equals the players' kills (691 of 694 matches), so fill it in - only
    # when the header gave nothing, never over a value that was read. Done here, before
    # any fingerprint, so the two confirming reads stay identical.
    if total_kills is None and players and all(p["kills"] is not None for p in players):
        total_kills = sum(p["kills"] for p in players)

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


def _csv_spool_path(path):
    """Side file holding rows that couldn't be appended to *path* yet."""
    root, ext = os.path.splitext(path)
    return f"{root}_unsaved{ext or '.csv'}"


def _read_csv_rows(path):
    """All rows of a CSV as dicts; [] when it doesn't exist."""
    if not os.path.exists(path):
        return []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def already_logged_ids(path):
    """Dedup keys for matches already in the CSV. For each match (its 3 rows share a
    session_id) it adds both the stat fingerprint and the normalized session id, so a
    match already logged - even under a differently-OCR'd id - is recognized. Count
    distinct matches via the 'stat:' keys."""
    seen = set()
    groups = {}
    # Rows still waiting in the side file (CSV was locked) count as logged too, or
    # a restart while the summary is still up would log the match a second time.
    for p in (path, _csv_spool_path(path)):
        try:
            rows = _read_csv_rows(p)
        except (OSError, ValueError, csv.Error) as e:
            # An unreadable CSV used to stop the watch from starting at all.
            _note(f"(CSV) couldn't read {os.path.basename(p)} for dedup ({e}); continuing.")
            continue
        for row in rows:
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


def _row_key(r):
    return (str(r.get("session_id") or ""), str(r.get("name") or ""), str(r.get("timestamp") or ""))


def _unsynced_path(path):
    """Matches whose Supabase insert failed, one JSON line each, retried later."""
    root, _ = os.path.splitext(path)
    return f"{root}_unsynced.jsonl"


def _supabase_insert(rows):
    """Insert one match's rows. Returns True on success. If the project hasn't run
    supabase_migration_ranked.sql, the `ranked` column is missing and PostgREST
    rejects the WHOLE insert (PGRST204); then `ranked` is stripped and the insert
    retried so the core stats still sync (the flag stays CSV-only until the column
    exists)."""
    def _insert(rs):
        # returning="minimal" avoids reading the rows back, so this works under an
        # insert-only RLS policy (no SELECT granted to anon).
        _SUPABASE_CLIENT.table("apex_matches").insert(rs, returning="minimal").execute()
    try:
        _insert(rows)
        return True
    except Exception as e:
        if "ranked" in str(e) and any("ranked" in r for r in rows):
            try:
                _insert([{k: v for k, v in r.items() if k != "ranked"} for r in rows])
                _note(f"(Supabase) synced {len(rows)} records WITHOUT 'ranked' - run "
                      f"supabase_migration_ranked.sql to store the ranked flag.")
                return True
            except Exception as e2:
                e = e2
        _note(f"(Supabase) Error syncing to Supabase: {e}")
        return False


def _sync_unsynced(path):
    """Retry matches whose earlier insert failed (network drop, Supabase down).
    Returns True when nothing is left waiting. A retry after an insert that had in
    fact landed leaves two same-session copies, which the dashboard collapses."""
    up = _unsynced_path(path)
    if not os.path.exists(up):
        return True
    try:
        with open(up, "r", encoding="utf-8") as f:
            groups = [json.loads(line) for line in f if line.strip()]
    except (OSError, ValueError) as e:
        _note(f"(Supabase) couldn't read {os.path.basename(up)}: {e}")
        return False
    left = [g for g in groups if not _supabase_insert(g)]
    if len(left) < len(groups):
        _note(f"(Supabase) synced {len(groups) - len(left)} match(es) that failed earlier.")
    try:
        if left:
            with open(up, "w", encoding="utf-8") as f:
                f.writelines(json.dumps(g) + "\n" for g in left)
        else:
            os.remove(up)
    except OSError as e:
        _note(f"(Supabase) couldn't update {os.path.basename(up)}: {e}")
    return not left


def append_match(path, match):
    """Write one match to the CSV and Supabase. Never raises for a sink failure:
    returns ``(csv_ok, db_ok)`` (``db_ok`` is None without a Supabase client)."""
    new_file = not os.path.exists(path)
    # Timezone-aware local time: .astimezone() attaches this PC's UTC offset, so the
    # Supabase TIMESTAMPTZ column converts it to the CORRECT UTC instant. Without the
    # offset, a naive datetime.now() gets stored AS-IF it were UTC, so two squadmates
    # in different timezones logged the same match an hour apart. The CSV now shows
    # local time with its offset (e.g. ...-04:00), which is unambiguous too.
    ts = datetime.now().astimezone().isoformat(timespec="seconds")
    # Only attach `ranked` when detection actually ran (True/False). Left None when
    # the default-off feature is disabled, so the key is omitted from the Supabase
    # insert entirely — a project that hasn't run supabase_migration_ranked.sql
    # keeps syncing fine (no PGRST204).
    ranked = match.get("ranked")
    supabase_rows = []
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
        supabase_rows.append(row_data)

    csv_ok = True
    with _CSV_LOCK:
        # A CSV that can't be written (open in Excel, which locks it; a bad
        # re-save) used to raise here, BEFORE the Supabase insert, so the match was
        # lost from both and the exception ended the watch for the night. Now the
        # rows wait in a side file that the next successful append merges back,
        # and the insert below always runs.
        spool = _csv_spool_path(path)
        try:
            # Upgrade a pre-`ranked` (or otherwise older) file to the full header
            # first, so appending wider rows can't produce a ragged file the patch
            # step chokes on.
            if not new_file:
                _ensure_canonical_header(path)
            backlog = _read_csv_rows(spool)
            if backlog and not new_file:
                # Rows already in the CSV are skipped, so a side file whose delete
                # failed after an earlier merge can never be merged twice.
                have = {_row_key(r) for r in _read_csv_rows(path)}
                backlog = [r for r in backlog if _row_key(r) not in have]
            with open(path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                if new_file:
                    w.writeheader()
                for r in backlog + supabase_rows:
                    w.writerow(r)
            if os.path.exists(spool):
                if backlog:
                    _note(f"(CSV) merged {len(backlog)} rows saved while the CSV was locked.")
                try:
                    os.remove(spool)
                except OSError:
                    pass
        except (OSError, ValueError, csv.Error) as e:
            csv_ok = False
            _note(f"(CSV) {os.path.basename(path)} not writable ({e}); match kept in "
                  f"{os.path.basename(spool)} until it is.")
            try:
                spool_new = not os.path.exists(spool)
                with open(spool, "a", newline="", encoding="utf-8") as f:
                    w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    if spool_new:
                        w.writeheader()
                    for r in supabase_rows:
                        w.writerow(r)
            except OSError as e2:
                _note(f"(CSV) could not save the match locally either: {e2}")

    # Network sync runs outside the CSV lock so it can't block RP-patch threads.
    if not _SUPABASE_CLIENT:
        return csv_ok, None
    earlier_ok = _sync_unsynced(path)
    if _supabase_insert(supabase_rows):
        print(f"[{datetime.now():%H:%M:%S}] (Supabase) synced {len(supabase_rows)} player records.")
        return csv_ok, earlier_ok
    # Keep it for a retry with the next match instead of losing it from the dashboard.
    try:
        with open(_unsynced_path(path), "a", encoding="utf-8") as f:
            f.write(json.dumps(supabase_rows) + "\n")
    except OSError as e:
        _note(f"(Supabase) couldn't queue the match for retry: {e}")
    return csv_ok, False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def open_live_capture(cfg):
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
    throttle = cap_cfg.get("throttle_ms", 500)
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
    False (pub), or None (detection disabled / never sampled). In RP v2 it is
    stored on the match and carried into the pending queue as a resolve-time HINT
    (``ranked_hint``) — it no longer GATES RP collection: every match gets a
    baseline + pending record, and the resolver classifies it from the ledger
    later (an observed delta proves ranked even if the badge was missed)."""
    match = extract_match(frame, cfg_eff, scale)
    # Wait until every player is named, every stat field read, AND placement has
    # rendered, so we never log a half-drawn screen. Names + placement alone is
    # not enough: a card whose K/A/Kn or damage row failed OCR returns None for
    # those, which earlier slipped through and logged blank-stat rows (e.g. a
    # player with empty kills/assists/knocks). Treat any None stat as "still
    # rendering" and retry. (None is distinct from a legit 0, which reads as 0.)
    # The reason a read can't be logged goes in status["read_reason"] (the return
    # value stays a bare 'incomplete': both loops treat anything else as "done with
    # this summary"), so a summary that never logs says why instead of vanishing.
    missing = []
    for p in match["players"]:
        if not p["name"]:
            missing.append(f"slot {p['player_slot']} name")
        missing += [f"slot {p['player_slot']} {k}"
                    for k in ("kills", "assists", "knocks", "damage") if p[k] is None]
    if match["squad_placed"] is None:
        missing.append("placement")
    if missing:
        status["read_reason"] = "unread: " + ", ".join(missing)
        return "incomplete"
    # Sanity gate: a mid-animation read (the stats roll up when the screen first
    # appears) or a misaligned region can yield physically-impossible values -
    # concatenated junk like 976561 damage or 1001 assists. Treat those as "not
    # done rendering" and retry, so garbage never gets logged. This also prevents
    # the double-log it caused: a garbage read has different stats AND a different
    # session-id than the good read, so dedup can't collapse the two.
    if not _stats_plausible(match):
        status["read_reason"] = "implausible: " + _implausible_detail(match)
        return "incomplete"
    if not match["session_id"]:
        match["session_id"] = match_key(match)  # persist so restarts dedup
    # Dedup on the stable stats, not the flaky session id. Skip if this match was
    # already logged - even if its id OCRs differently this time.
    sk = sid_norm(match["session_id"])
    fp = stat_fingerprint(match["players"], match["squad_placed"],
                          match["total_squad_kills"])
    if fp in seen or (sk and sk in seen):
        status["read_reason"] = None
        return "duplicate"

    # Stability gate: only log once these exact stats are confirmed by a NEWER
    # capture frame, so mid-animation values (plausible but still rolling up — the
    # summary slides/rolls in over a couple seconds) are never committed. The
    # `frame_ts > stable["ft"]` check requires the confirmation to come from a
    # genuinely new capture, not a re-read of the same cached frame — and with the
    # capture throttle the two confirming reads are spaced far enough apart that
    # the screen has finished animating by the second one. (This spacing is what
    # kept reads reliable; a faster path here causes mis-read names/numbers.)
    if stable is not None:
        confirmed = (fp == stable.get("fp") and frame_ts is not None
                     and frame_ts > stable.get("ft", 0.0))
        if not confirmed:
            stable["fp"] = fp
            stable["ft"] = frame_ts if frame_ts is not None else 0.0
            status["read_reason"] = "waiting for a newer frame to confirm the read"
            return "incomplete"

    status["read_reason"] = None
    match["ranked"] = ranked
    # v2 (RP_TRACKING_V2_SPEC §4.2): collect RP evidence for EVERY logged match,
    # pub or ranked. The badge verdict is now only a resolve-time HINT, never a
    # gate — a badge false-negative on a real ranked game used to lose its RP
    # forever. So whenever the rank tracker is running we snapshot a baseline and
    # queue the match; the resolver classifies it later from the ledger (a nonzero
    # delta proves ranked; a zero-confirmed pub simply resolves to null/ranked=false).
    do_rp = bool(_RANK_TRACKER)

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

    csv_ok, db_ok = append_match(path, match)
    status["csv_problem"] = (None if csv_ok else
                             "CSV locked or unwritable (open in Excel?) - matches are kept in "
                             f"{os.path.basename(_csv_spool_path(path))} until it can be written.")
    status["db_problem"] = (None if db_ok is not False else
                            "Supabase sync failed - the match is saved locally and retried "
                            "with the next one.")
    seen.add(fp)
    if sk:
        seen.add(sk)
    status["logged_this_run"] += 1
    # Every roster player in the match without an RP baseline means this match will
    # get no RP at all - say so next to it rather than leave a silent blank.
    rostered = [p for p in match["players"] if p["name"] in (cfg_eff.get("known_names") or [])]
    rp_note = None
    if rostered and all(p.get("starting_rp") is None for p in rostered):
        state, detail = _rp_state()
        rp_note = (f"no RP: {detail}" if state in ("off", "failing")
                   else "no RP baseline from ALS yet")
    status["last_match"] = {
        "session_id": match["session_id"],
        "placed": match["squad_placed"],
        "total_kills": match["total_squad_kills"],
        "ranked": ranked,  # True / False / None(detection off) - shown by front-ends
        "time": datetime.now().strftime("%H:%M:%S"),
        "rp_note": rp_note,
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


class _SummaryEpisode:
    """One summary screen, from its first text-confirmed read until it is logged
    (or recognised as already logged) or leaves the screen.

    A summary that left unlogged used to leave no trace, so misses were noticed
    hours later with nothing to recover them from. Now the reason is written to the
    debug log, a failure that persists while the summary is still up is shown in the
    app (so it can be captured by hand), and a summary that leaves unlogged has its
    last frame saved to debug/unlogged/ with a note of why. Only frames whose banner
    TEXT confirmed a summary count, never banner colour alone (it also fires in
    gameplay). A miss is only reported once final: the summary can come back and log
    (tabbed away for a moment, or an on-demand re-probe), so it waits CONFIRM_MISS."""

    WARN_AFTER = 15.0     # seconds of failed reads in one hold before the app shows it
    CONFIRM_MISS = 120.0  # a summary that left unlogged must stay gone this long
    KEEP = 30             # unlogged frames kept on disk

    def __init__(self, status, emit, dbg):
        self.status, self.emit, self.dbg = status, emit, dbg
        self.pending = None  # a summary that left unlogged, waiting out CONFIRM_MISS
        self._clear()

    def _clear(self):
        self.started = None
        self.frame = None
        self.reads = 0
        self.reason = None
        self.warn_since = None
        self._logged_kind = (None, 0.0)

    def resume(self):
        """A new hold on the summary began (on-demand re-probe): the warning only
        counts time spent actually reading it in this hold."""
        self.warn_since = None

    def read(self, frame, result):
        """Record one _try_log_match() result for a text-confirmed frame."""
        now = time.time()
        if result != "incomplete":
            if self.pending is not None:
                self.dbg("  the summary came back and logged; not a miss")
                self.pending = None
            if self.status.get("unread_summary"):
                self.status["unread_summary"] = None
                self.emit("tick")
            self._clear()
            return
        if self.started is None:
            self.started = now
            if self.pending is not None:  # the summary that left came back: one episode
                self.started = now - self.pending["held"]
                self.reads = self.pending["reads"]
                self.pending = None
        if self.warn_since is None:
            self.warn_since = now
        self.frame, self.reads = frame, self.reads + 1
        reason = self.status.get("read_reason") or "unknown"
        self.reason = reason
        # Log on a change of kind (the text before ':') or every 10 s; the values in
        # a reason ('slot 1 kills 71717') can differ on every read.
        kind = reason.split(":")[0]
        if kind != self._logged_kind[0] or now - self._logged_kind[1] >= 10:
            self.dbg(f"  not logged yet: {reason}")
            self._logged_kind = (kind, now)
        if now - self.warn_since >= self.WARN_AFTER and self.status.get("unread_summary") != reason:
            if not self.status.get("unread_summary"):
                self.dbg(f"  summary up {now - self.started:.0f}s and still not logging: {reason}")
            self.status["unread_summary"] = reason
            self.emit("unread")

    def end(self, why, final=False):
        """Called while the summary is off screen (repeatedly is fine) and with
        final=True when the watch stops. Parks an unlogged summary, and reports it
        once it has stayed gone for CONFIRM_MISS (immediately when final)."""
        now = time.time()
        if self.started is not None:
            if self.pending is not None:
                self._report(self.pending)
            self.pending = {"why": why, "left": now, "reason": self.reason, "reads": self.reads,
                            "held": now - self.started, "frame": self.frame}
            if self.status.get("unread_summary"):
                self.status["unread_summary"] = None
                self.emit("tick")
            self._clear()
        if self.pending is not None and (final or now - self.pending["left"] >= self.CONFIRM_MISS):
            self._report(self.pending)
            self.pending = None

    def _report(self, p):
        saved = self._save(p)
        msg = (f"SUMMARY NOT LOGGED ({p['why']}) after {p['reads']} reads over {p['held']:.0f}s: "
               f"{p['reason']}" + (f" - frame saved to {saved}" if saved else ""))
        self.dbg(msg)
        try:
            print(f"[{datetime.now():%H:%M:%S}] {msg}")
        except Exception:
            pass
        self.status["missed"] = {"time": datetime.fromtimestamp(p["left"]).strftime("%H:%M:%S"),
                                 "reason": p["reason"], "frame": saved}
        self.emit("missed")

    def _save(self, p):
        try:
            out_dir = os.path.join(DEBUG_DIR, "unlogged")
            os.makedirs(out_dir, exist_ok=True)
            stem = os.path.join(out_dir, f"unlogged_{datetime.fromtimestamp(p['left']):%Y%m%d_%H%M%S}")
            # imencode + tofile: checked, and safe on a non-ASCII install path.
            ok, buf = cv2.imencode(".jpg", p["frame"], [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                raise OSError("JPEG encode failed")
            buf.tofile(stem + ".jpg")
            with open(stem + ".json", "w", encoding="utf-8") as f:
                json.dump({"reason": p["reason"], "ended": p["why"], "reads": p["reads"],
                           "held_seconds": round(p["held"], 1)}, f, indent=1)
            frames = sorted(n for n in os.listdir(out_dir) if n.endswith(".jpg"))
            for old in frames[:-self.KEEP]:
                for ext in (".jpg", ".json"):
                    try:
                        os.remove(os.path.join(out_dir, old[:-4] + ext))
                    except OSError:
                        pass
            return stem + ".jpg"
        except Exception as e:
            self.dbg(f"  could not save the unlogged frame: {e}")
            return None


def _report_loop_error(status, emit, dbg, n):
    """Record an exception from one watch-loop pass. The loops keep running after
    one (a locked file or a capture hiccup used to end the watch for the night while
    the app still looked alive). ``n`` counts consecutive failed passes; tracebacks
    are rate-limited and the GUI shows the error as active until a pass completes."""
    import traceback
    exc = sys.exc_info()[1]
    msg = f"{type(exc).__name__}: {exc}"
    status["loop_error"] = {"msg": msg, "count": n, "active": True,
                            "time": datetime.now().strftime("%H:%M:%S")}
    if n <= 3 or n % 20 == 0:
        dbg(f"ERROR in watch loop (#{n}): {traceback.format_exc()}")
        try:
            print(f"[{datetime.now():%H:%M:%S}] watch loop error #{n}: {msg}")
        except Exception:
            pass
    emit("tick")


def _loop_pass_ok(status):
    """A watch-loop pass completed: an earlier error is no longer active."""
    err = status.get("loop_error")
    if err and err.get("active"):
        err["active"] = False


def _episode_error(episode, status):
    """An exception during a summary that was already being read counts against
    that summary, so a read that keeps throwing ends up reported like any miss."""
    if episode.started is not None:
        try:
            status["read_reason"] = f"error: {status['loop_error']['msg']}"
            episode.read(episode.frame, "incomplete")
        except Exception:
            pass


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
#   {"session_id","name","player_slot","starting_rp","ranked_hint","csv_path",
#    "logged_epoch"}
_RP_PENDING_LOCK = threading.Lock()
_RP_RESOLVER_STARTED = False
_RP_RESOLVER_STOP = threading.Event()


def _rp_pending_path():
    return os.path.join(HERE, "rp_pending.json")


def _rp_chain_path():
    return os.path.join(HERE, "rp_chain.json")


def _load_rp_chain():
    """Per-player resolution anchors ``{name: {"end": rp, "ts": obs_ts}}``.

    Persisted so baseline chaining survives sweeps, restarts, and even a cleared
    pending queue — losing the chain is exactly what let one RP movement get
    attributed to two consecutive matches. Caller holds _RP_PENDING_LOCK."""
    path = _rp_chain_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_rp_chain(chain):
    """Atomic write, same pattern as the pending queue. Caller holds the lock."""
    path = _rp_chain_path()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(chain, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


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
    match_fp = _match_fp(match["squad_placed"], match["total_squad_kills"], match["players"])
    new_records = [{
        "session_id": match["session_id"],
        "name": p["name"],
        "player_slot": p["player_slot"],
        "starting_rp": p["starting_rp"],
        # Resolve-time HINT (badge verdict), not a gate: True/False/None. The
        # resolver overrides it when a nonzero delta proves the match was ranked.
        "ranked_hint": match.get("ranked"),
        "csv_path": csv_path_str,
        "logged_epoch": now,
        # Lets the Supabase patch find this match's surviving row when the DB's
        # cross-PC dedup trigger deleted this PC's copy (see _patch_rp_surviving_copy).
        "damage": p.get("damage"),
        "kills": p.get("kills"),
        "match_fp": match_fp,
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
    """One pass over the pending queue (RP_TRACKING_V2_SPEC §4.2).

    Refresh ALS for the players still awaiting RP (which appends fresh readings to
    the always-on ledger), then hand the pending records + the ledger observations
    to the pure resolver (:func:`rp_resolver.resolve_rp`), which classifies each
    player's EARLIEST pending match at resolve time: a nonzero delta proves ranked;
    a zero confirmed over a full window is a genuine 0 (badge tiebreaks); collapsed
    or reset-spanning gaps are flagged, never guessed. This impure wrapper only
    does I/O — poll, load, apply patches, rewrite the durable queue.

    The queue, locks, atomic writes and patchers are unchanged; only the
    resolution *rules* moved into the tested pure function."""
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
    zero_polls = cfg.get("rp_zero_confirm_polls", 6)
    zero_secs = cfg.get("rp_zero_confirm_seconds", 360)
    expiry_secs = cfg.get("rp_pending_expiry_hours", 24) * 3600
    now = time.time()

    fresh = [r for r in records if (now - r.get("logged_epoch", now)) < expiry_secs]
    for r in records:
        if r not in fresh:
            # Expired: RP never propagated within the (generous) window. Leave an
            # honest blank — assume-zero is deprecated (zero-confirm supersedes it).
            _note(f"(ALS) RP expired unresolved for {r['session_id']} / {r['name']} "
                  f"(left blank).")

    # Poll the players still awaiting RP so the ledger has a fresh reading; this
    # is the only side effect that feeds new evidence to the resolver.
    names = sorted({r["name"] for r in fresh})
    if names:
        try:
            _RANK_TRACKER.force_poll_names(names)
        except Exception as exc:
            print(f"[{datetime.now():%H:%M:%S}] (ALS) RP poll error - {exc}")

    # Gather each pending player's ledger observations from well before their
    # earliest pending match: the pre-log history feeds the zero-confirm
    # pre-match-evidence guard and the chain-anchor validity checks (untracked
    # play / reset detection in the gap).
    with _RP_PENDING_LOCK:
        chain = _load_rp_chain()
    observations = {}
    for name in names:
        earliest = min(r.get("logged_epoch", now) for r in fresh if r["name"] == name)
        since = earliest - 1800
        anchor_ts = (chain.get(name) or {}).get("ts")
        if anchor_ts:
            since = min(since, anchor_ts - 60)
        observations[name] = _RANK_TRACKER.get_observations(name, since)

    actions = resolve_rp(fresh, observations, now, loss_floor=loss_floor,
                         zero_confirm_polls=zero_polls, zero_confirm_seconds=zero_secs,
                         chain=chain)

    by_key = {(r.get("session_id"), r.get("name")): r for r in fresh}
    for patch in actions.patches:
        cols = patch["set"]
        # Last-ditch invariants: nothing beyond a believable single-game loss and
        # no non-positive RP totals ever reach the CSV/DB, no matter what upstream
        # produced them (a bogus ALS reading, a poisoned baseline, an old build's
        # queue). Blocked patches resolve as honest blanks, loudly.
        chg = cols.get("rp_change")
        end = cols.get("ending_rp")
        if (chg is not None and chg < loss_floor) or (end is not None and end <= 0):
            _note(f"(ALS) BLOCKED implausible RP patch for {patch['session_id']} / "
                  f"{patch['name']}: {cols} (left blank).")
            continue
        _safe_patch_rp(patch.get("csv_path"), patch["session_id"],
                       {patch["name"]: cols},
                       hints={patch["name"]: by_key.get((patch["session_id"], patch["name"]))})
        _note(f"(ALS) RP resolved for {patch['session_id']} / {patch['name']}: {cols}.")
    for note in actions.notes:
        _note(f"(ALS) {note}")
    # Propagation latency (first_changed_ts - logged_epoch) — the tuning
    # instrument for cadence/windows (§4.2). Debug-level; only if enabled.
    if actions.latencies and cfg.get("capture", {}).get("debug_log"):
        dbg = _make_debug_logger(cfg, header=False)
        for sid, name, secs in actions.latencies:
            dbg(f"RP propagation latency {sid}/{name}: {secs:.0f}s")

    resolved_keys = set(actions.resolved)
    rebaseline = {k: v for k, v in actions.rebaselines.items()}

    # Reconcile under the lock (a concurrent enqueue may have appended): drop
    # resolved + expired, apply chained baselines, persist the chain anchors,
    # keep everything else.
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
        if actions.chain_updates:
            chain = _load_rp_chain()  # reload: another sweep may have written
            chain.update(actions.chain_updates)
            try:
                _save_rp_chain(chain)
            except Exception as exc:
                print(f"[{datetime.now():%H:%M:%S}] (ALS) chain save failed - {exc}")


def _safe_patch_rp(csv_path_str, session_id, resolved, hints=None):
    """Apply a resolved-RP patch to both the CSV and Supabase, isolating each so a
    failure in one (e.g. a CSV rewrite hiccup) is logged but never aborts the
    resolver thread or skips the other sink. ``hints`` is ``{name: pending
    record}`` (damage/kills/logged_epoch) for the Supabase fallback."""
    try:
        _patch_rp_in_csv(csv_path_str, session_id, resolved)
    except Exception as e:
        _note(f"(ALS) CSV RP patch failed for {session_id}: {e}")
    try:
        _patch_rp_in_supabase(session_id, resolved, hints)
    except Exception as e:
        _note(f"(ALS) Supabase RP patch failed for {session_id}: {e}")


def _patch_rp_in_csv(csv_path_str, session_id, resolved):
    """Re-read the CSV, update matching rows, rewrite.

    *resolved* is ``{name: {col: val}}`` where ``col`` is any of ``ending_rp`` /
    ``rp_change`` / ``ranked`` — only the columns present are written, so a real
    value is never overwritten with a fabricated one. Only rows whose
    ``session_id`` matches are touched. Rows still waiting in the side file (the
    CSV was locked when they logged) are patched there, so they merge back with RP.
    """
    # Hold the lock across the whole read-modify-rewrite so a concurrent
    # append_match() (or another patch thread) can't clobber the file.
    with _CSV_LOCK:
        err = None
        for p in (csv_path_str, _csv_spool_path(csv_path_str)):
            if os.path.exists(p):
                try:
                    _patch_rp_in_csv_file(p, session_id, resolved)
                except Exception as e:  # one locked file mustn't block the other
                    err = err or e
        if err:
            raise err


def _patch_rp_in_csv_file(csv_path_str, session_id, resolved):
    """One file's read-modify-rewrite for _patch_rp_in_csv (caller holds _CSV_LOCK)."""
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
            for col, val in resolved[row["name"]].items():
                row[col] = val
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
    try:
        os.replace(tmp, csv_path_str)
    except OSError:
        try:
            os.remove(tmp)  # e.g. the CSV is open in Excel: don't leave .tmp behind
        except OSError:
            pass
        raise


def _patch_rp_in_supabase(session_id, resolved, hints=None):
    """Update Supabase rows with the resolved columns.

    *resolved* is ``{name: {col: val}}`` where ``col`` is any of ``ending_rp`` /
    ``rp_change`` / ``ranked``; only the present columns are written. The resolver
    may now flip ``ranked`` async (a delta proving a match was ranked), exactly
    like ``rp_change`` — the dashboard's dedupeRows already coalesces both.
    """
    if not _SUPABASE_CLIENT:
        return
    for name, cols in resolved.items():
        if not cols:
            continue
        try:
            resp = _SUPABASE_CLIENT.table("apex_matches").update(dict(cols)) \
                .eq("session_id", session_id).eq("name", name).execute()
        except Exception as e:
            # Friend builds patch on the scoped anon key. Until
            # supabase_migration_rp_v2.sql extends that grant to `ranked`, an
            # update including `ranked` is rejected — retry without it so the
            # RP columns still land (the flag stays until the migration runs).
            retry = {k: v for k, v in cols.items() if k != "ranked"}
            if "ranked" in cols and retry and "ranked" in str(e):
                try:
                    _SUPABASE_CLIENT.table("apex_matches").update(retry) \
                        .eq("session_id", session_id).eq("name", name).execute()
                    _note(f"(Supabase) RP patch for {name} synced WITHOUT 'ranked' - run "
                          f"supabase_migration_rp_v2.sql to let the resolver update the "
                          f"ranked flag.")
                    continue
                except Exception as e2:
                    e = e2
            _note(f"(Supabase) RP patch error for {name}: {e}")
            continue
        # Only the service key can read rows back; on the anon key an update that
        # worked returns no rows too, so "0 rows" means something only here.
        if _SUPABASE_IS_SERVICE and not (resp.data or []):
            try:
                _patch_rp_surviving_copy(session_id, name, cols, (hints or {}).get(name))
            except Exception as e:
                _note(f"(Supabase) surviving-copy RP patch failed for {name} / {session_id}: {e}")


def _match_fp(squad_placed, total_squad_kills, rows):
    """The dedup trigger's content fingerprint of one match (placement | total
    kills | sorted damage:kills), with a NULL total read as sum(kills) the way
    supabase_dedup_v2.sql and the dashboard do. None for a match with no damage,
    which is too featureless to identify."""
    if not rows or not sum(r.get("damage") or 0 for r in rows):
        return None
    tk = total_squad_kills
    if tk is None:
        tk = sum(r.get("kills") or 0 for r in rows)
    pairs = ",".join(sorted(f"{r.get('damage') or 0}:{r.get('kills') or 0}" for r in rows))
    return f"{'' if squad_placed is None else squad_placed}|{tk}|{pairs}"


def _patch_rp_surviving_copy(session_id, name, cols, hint):
    """RP patch fallback for when this PC's copy of a match no longer exists.

    When two squadmates' trackers log the same game, the DB dedup trigger
    (supabase_dedup.sql) keeps the first copy and DELETES the later one, so this
    PC's patch by its own session id matched 0 rows and the match kept NULL RP with
    no error (the other copy often comes from a PC without RP). The trigger only
    deletes on an identical content fingerprint, so the survivor is found the same
    way: exactly one OTHER session within 15 min whose fingerprint equals this
    match's. Within it the player is matched by name, else by a damage:kills pair
    unique in that session. Only blank RP is ever written."""
    if not hint or not hint.get("match_fp") or hint.get("logged_epoch") is None:
        _note(f"(Supabase) RP patch for {name} / {session_id} matched 0 rows "
              f"(no match fingerprint to find another copy; left blank).")
        return
    t0 = hint["logged_epoch"]
    lo = datetime.fromtimestamp(t0 - 900, timezone.utc).isoformat()
    hi = datetime.fromtimestamp(t0 + 900, timezone.utc).isoformat()
    rows = (_SUPABASE_CLIENT.table("apex_matches")
            .select("id,session_id,name,damage,kills,squad_placed,total_squad_kills,rp_change")
            .gte("timestamp", lo).lte("timestamp", hi).execute().data or [])
    sessions = {}
    for r in rows:
        if r.get("session_id") != session_id:
            sessions.setdefault(r["session_id"], []).append(r)
    same = [rs for rs in sessions.values()
            if _match_fp(rs[0].get("squad_placed"), rs[0].get("total_squad_kills"), rs)
            == hint["match_fp"]]
    if len(same) != 1:
        _note(f"(Supabase) RP patch for {name} / {session_id} matched 0 rows and "
              f"{len(same)} other copies of the match; left blank.")
        return
    mates = same[0]
    row = next((r for r in mates if r.get("name") == name), None)
    if row is None:  # the other PC OCR'd the name differently ('ullvautnunter')
        pair = [r for r in mates
                if (r.get("damage"), r.get("kills")) == (hint.get("damage"), hint.get("kills"))]
        row = pair[0] if len(pair) == 1 else None
    if row is None or row.get("rp_change") is not None:
        _note(f"(Supabase) RP patch for {name} / {session_id}: no unambiguous blank row in "
              f"the surviving copy {mates[0]['session_id']}; left blank.")
        return
    _SUPABASE_CLIENT.table("apex_matches").update(dict(cols)) \
        .eq("id", row["id"]).is_("rp_change", "null").execute()
    _note(f"(Supabase) RP for {name} written to the surviving copy {row['session_id']} "
          f"(this PC's copy {session_id} was removed as a cross-PC duplicate).")


def _rank_status_lines():
    """Per-player live rank + session RP for the status panel / heartbeat (§4.3).

    Returns ``[{name, rp, tier, div, session_delta}, ...]`` in roster (poll) order.
    ``session_delta`` is the self-healing ``latest − session_baseline`` number
    (TAS parity), None until the player has a reading this session. Empty list
    when no rank tracker is running."""
    if _RANK_TRACKER is None:
        return []
    lines = []
    for name, e in _RANK_TRACKER.get_all().items():
        delta, _, _ = _RANK_TRACKER.get_session_delta(name)
        lines.append({
            "name": name,
            "rp": e.get("current_rp"),
            "tier": e.get("rank_name"),
            "div": e.get("rank_div"),
            "session_delta": delta,
        })
    return lines


def _pending_status_lines(warn_secs, now=None):
    """Pending RP records unresolved longer than *warn_secs* — a real-time signal
    that a match is stuck in an abnormal state.

    600s (default) clears the legitimate slow path (zero-confirm floor ~360s + EA's
    ~2-3 min cache), so a line here means something genuinely off (ALS never moved,
    a player unresolvable, a collapse/reset still churning). Returns
    ``[{name, session_id, age, starting_rp}, ...]``; empty when warn_secs<=0."""
    if not warn_secs or warn_secs <= 0:
        return []
    now = now if now is not None else time.time()
    with _RP_PENDING_LOCK:
        try:
            records = _load_rp_pending()
        except Exception:
            records = []
    stuck = []
    for r in records:
        age = now - r.get("logged_epoch", now)
        if age >= warn_secs:
            stuck.append({
                "name": r.get("name"),
                "session_id": r.get("session_id"),
                "age": age,
                "starting_rp": r.get("starting_rp"),
            })
    return stuck


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
    global _DIAG
    stop_event = stop_event or _th.Event()
    sync_roster_to_supabase(cfg)
    # (Re)build the ALS rank tracker from the current config so an API key / roster
    # entered in Settings applies now (Stop->Start) without an app restart, then
    # resume resolving any RP left pending from a previous session (durable queue).
    init_rank_tracker(cfg)
    # A session = this watch run (Start→Stop). Anchor the session-RP baseline now,
    # even if the tracker object was reused across a Stop→Start (§4.3).
    if _RANK_TRACKER is not None:
        _RANK_TRACKER.set_session_start(time.time())
    _ensure_rp_resolver()
    forced = forced_res or cfg.get("force_resolution") or None
    path = csv_path(cfg)
    _DIAG = _make_debug_logger(cfg, header=False)  # so the dedup read below can report
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
              "profile": None, "match_ranked": False, "event": "start",
              # Per-player live rank + session RP (§4.3); refreshed on heartbeat.
              "players_rp": [], "rp_untracked": [], "rp_stuck": [],
              # RP tracking health ('off'/'starting'/'ok'/'failing', detail), why the
              # current summary isn't logging, a summary that is on screen but has
              # failed to log for a while, the last summary that never logged, the
              # last exception a loop pass survived ({msg, count, active, time}), and
              # a CSV / Supabase write that is failing.
              "rp_state": _rp_state(), "read_reason": None, "unread_summary": None,
              "missed": None, "loop_error": None, "csv_problem": None, "db_problem": None}

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

    # OBS mode reads a continuous virtual-camera feed with ZERO game overhead, so
    # poll faster + settle shorter than the monitor/on-demand defaults to catch a
    # quickly-dismissed summary (during a summary each loop runs OCR ~0.4s and then
    # waited the full 1s poll, so reads landed ~1.5s apart — too slow). Reads stay
    # correct: the stability gate still requires the SAME stats confirmed by a
    # second fresh frame, so a mid-animation value is still rejected — we just
    # check more often. Tunable via obs_poll_seconds / obs_settle_seconds.
    if cap_mode == "obs":
        poll = cfg.get("obs_poll_seconds", 0.3)

    dbg = _make_debug_logger(cfg)
    _DIAG = dbg
    dbg(f"continuous watch started; mode={cap_mode} poll={poll}s csv={path}")
    dbg("RP tracking: %s%s" % (status["rp_state"][0],
                               f" ({status['rp_state'][1]})" if status["rp_state"][1] else ""))
    episode = _SummaryEpisode(status, emit, dbg)
    errors = 0
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
    settle = (cfg.get("obs_settle_seconds", 0.8) if cap_mode == "obs"
              else cfg.get("settle_seconds", 1.5))
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
            failed = False
            try:
                age = cap.last_frame_age()
                status["frame_age"] = age
                status["state"] = "watching" if age < stale_after else "stale"
                # Cheaply re-acquire only when needed. During normal play this is just
                # an IsWindow() check (no expensive window scan), so it won't hitch.
                # OBS mode checks more often + at a lower staleness threshold: a frozen
                # virtual-camera feed is the ONLY failure signal there (no window to
                # probe), and a slow recovery is exactly how a summary gets missed —
                # the feed stalled ~65s once and a game didn't log. Faster recovery
                # (re-acquire within ~8-12s) catches the summary while it's still up.
                recheck_every = 8 if cap_mode == "obs" else 30
                stale_reacquire = 12 if cap_mode == "obs" else 20
                if time.time() - last_recheck > recheck_every:
                    last_recheck = time.time()
                    if cap_mode == "obs":
                        # OBS reads a virtual camera, NOT the Apex window — so a missing
                        # game window is normal (e.g. tracking on a separate stream PC
                        # where the game isn't running). Re-acquire only when the feed
                        # actually goes stale, never just because there's no hwnd.
                        need_reacquire = cap.last_frame_age() > stale_reacquire
                    else:
                        alive = hwnd is not None and _user32.IsWindow(hwnd)
                        need_reacquire = (not alive) or cap.last_frame_age() > 20
                    if need_reacquire:
                        cap.release()
                        cap, src, hwnd = open_live_capture(cfg)
                        status["src"] = src
                        dbg(f"re-acquired capture -> {src}")
                        emit("reacquire")
                # Heartbeat tick so a front-end can show it is alive and capturing.
                if hb_secs and time.time() - last_hb >= hb_secs:
                    last_hb = time.time()
                    status["players_rp"] = _rank_status_lines()          # §4.3
                    status["rp_untracked"] = (_RANK_TRACKER.unresolved_names()
                                              if _RANK_TRACKER is not None else [])
                    status["rp_stuck"] = _pending_status_lines(
                        cfg.get("rp_pending_warn_seconds", 600))
                    rp_state = _rp_state()
                    if rp_state != status["rp_state"]:
                        dbg(f"RP tracking: {rp_state[0]}" + (f" ({rp_state[1]})" if rp_state[1] else ""))
                    status["rp_state"] = rp_state
                    emit("heartbeat")
                else:
                    emit("tick")

                frame, frame_ts = cap.grab_with_ts()
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
                        episode.end("summary left the screen")
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
                                        stable=stable, frame_ts=frame_ts)
                episode.read(frame, result)
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
            except Exception:
                failed = True
                errors += 1
                _report_loop_error(status, emit, dbg, errors)
                _episode_error(episode, status)
                stop_event.wait(min(30.0, poll * 2 ** min(errors, 5)))
            finally:
                if not failed:  # also runs on every `continue`: the pass completed
                    errors = 0
                    _loop_pass_ok(status)
    finally:
        episode.end("watch stopped", final=True)
        cap.release()
        dbg("continuous watch stopped")
        status["state"] = "stopped"
        emit("stop")
    return status["logged_this_run"]


def _make_debug_logger(cfg, header=True):
    """Return a dbg(msg) that appends timestamped lines to debug/tracker_debug.log
    when capture.debug_log is true, else a no-op. Lets you alt-tab out after a
    session and see exactly when the tracker opened capture, what it saw, and when
    it idled - so you can line probes up against any in-game stutter you felt.

    ``header=False`` skips the '--- session start ---' banner — the RP resolver
    sweep makes an ephemeral logger every pass, and writing the banner each time
    spammed the log with bogus 'session start' lines that looked like restarts."""
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

    if header:
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
    global _DIAG
    idle_probe = cfg.get("capture", {}).get("idle_probe_seconds", 8)
    dbg = _make_debug_logger(cfg)
    _DIAG = dbg
    dbg(f"on-demand watch started; idle_probe={idle_probe}s settle={settle_seconds}s "
        f"poll={poll}s csv={path}")
    dbg("RP tracking: %s%s" % (status["rp_state"][0],
                               f" ({status['rp_state'][1]})" if status["rp_state"][1] else ""))
    episode = _SummaryEpisode(status, emit, dbg)
    errors = 0
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
            status["players_rp"] = _rank_status_lines()          # §4.3
            status["rp_untracked"] = (_RANK_TRACKER.unresolved_names()
                                      if _RANK_TRACKER is not None else [])
            status["rp_stuck"] = _pending_status_lines(
                cfg.get("rp_pending_warn_seconds", 600))
            rp_state = _rp_state()
            if rp_state != status["rp_state"]:
                dbg(f"RP tracking: {rp_state[0]}" + (f" ({rp_state[1]})" if rp_state[1] else ""))
            status["rp_state"] = rp_state
            emit("heartbeat")
        else:
            emit("tick")

    probe_n = 0
    try:
        while not stop_event.is_set():
            probe_n += 1
            cap = None
            failed = False
            try:
                t_open = time.time()
                cap, src, hwnd = open_live_capture(cfg)
                status["src"] = src
                got = cap.wait_first(5)
                frame = cap.grab() if got else None
                open_ms = (time.time() - t_open) * 1000
                if frame is None:
                    cap.release()
                    episode.end("capture lost")
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
                    episode.end("summary left the screen")
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
                episode.resume()
                while not stop_event.is_set():
                    frame, frame_ts = cap.grab_with_ts()
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
                                            stable=stable, frame_ts=frame_ts)
                    episode.read(frame, result)
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
                # An unlogged summary stays open across the idle gap: the hold loop
                # leaves on one banner-less frame and the next probe often finds the
                # same summary still up and logs it. It ends at the next gameplay probe.
                cap.release()
                dbg(f"probe #{probe_n}: released after summary read, idling {idle_probe}s (capture OFF)")
                # Idle again. If we just logged, this also waits out part of the summary
                # so the next probe doesn't immediately re-read the same (now deduped)
                # screen more than necessary.
                stop_event.wait(idle_probe)
            except Exception:
                failed = True
                errors += 1
                _report_loop_error(status, emit, dbg, errors)
                _episode_error(episode, status)
                if cap is not None:
                    cap.release()  # never hold capture open through the backoff
                stop_event.wait(min(120.0, idle_probe * 2 ** min(errors, 3)))
            finally:
                if cap is not None:
                    cap.release()  # idempotent; also covers an exception mid-probe
                if not failed:
                    errors = 0
                    _loop_pass_ok(status)
    finally:
        episode.end("watch stopped", final=True)
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
            line = (f"[{datetime.now():%H:%M:%S}] still watching {s['src']} | {health} | "
                    f"{s['logged_this_run']} logged this run")
            # Keep any untracked roster player visible: a name ALS can't resolve
            # logs match stats but never gets RP, so remind every heartbeat.
            missing = s.get("rp_untracked") or []
            if missing:
                line += f" | RP UNTRACKED: {', '.join(missing)} (not found on ALS)"
            print(line)
            # Per-player live rank + self-healing session RP (§4.3).
            for pr in s.get("players_rp", []):
                d = pr.get("session_delta")
                sess = f"session {d:+d}" if d is not None else "session --"
                tier = pr.get("tier") or "?"
                div = f" {pr['div']}" if pr.get("div") else ""
                rp = pr.get("rp")
                rp_s = f"{rp} RP" if rp is not None else "RP --"
                print(f"    {pr['name']:<16} {tier}{div}  {rp_s}  {sess}")
            # Matches stuck in RP resolution past the warn threshold — abnormal.
            for st in s.get("rp_stuck", []):
                start = st.get("starting_rp")
                start_s = f"start RP {start}" if start is not None else "no baseline"
                print(f"    ! RP still pending: {st['name']} / {st['session_id']} "
                      f"- {st['age'] / 60:.0f} min unresolved ({start_s})")

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
