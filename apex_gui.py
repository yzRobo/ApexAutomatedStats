"""
Apex Tracker - desktop UI.

A small Tkinter front-end over apex_tracker.run_watch: Start/Stop the passive
watcher, see a live status/heartbeat, edit the squad roster (gamertags + optional
ALS UIDs) and resolution without touching config.json, check for updates, and open
the stats dashboard. It stays strictly passive - it only drives the existing
screen-reader; it never touches the game.

Run from source:  py apex_gui.py
Frozen:           ApexTrackerUI.exe
"""

import os
import sys
import threading
import webbrowser
from datetime import datetime

# A windowed (--noconsole) PyInstaller build has no stdout/stderr; library print()
# calls would then crash. Point them at a sink before anything else runs.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import tkinter as tk
from tkinter import ttk, messagebox

try:
    import sv_ttk  # Sun Valley (Win11-style) ttk theme
except ImportError:
    sv_ttk = None

import apex_tracker as core

# --------------------------------------------------------------------------- #
# Theme - a flat dark palette. Every widget is styled (incl. the classic Tk
# Canvas), so there are no default-grey patches. Status hues are kept distinct
# from the accent so colour still reads as meaning, not decoration.
# --------------------------------------------------------------------------- #
BG = "#16181d"          # window background
SURFACE = "#1c1f26"     # raised sections (labelframes)
INPUT = "#252932"       # entries / lists / buttons
BORDER = "#333842"
TEXT = "#e7e9ec"
MUTED = "#9aa0a6"
ACCENT = "#4f8cff"      # primary action
ACCENT_HOVER = "#6a9dff"
GREEN = "#3fb950"
AMBER = "#e3a23c"
RED = "#f0563a"
GREY = "#7d828c"

# Display-ratio picker. Resolution is auto-detected and auto-scaled, so the only
# thing the user needs to choose is the in-game aspect / layout. Each maps to a
# force_resolution value: "16:9" -> the native-16:9 calibration profile; "16:10"
# -> "" (the base regions, which are tuned for 16:10-in-game on a 16:9 monitor).
NATIVE_16x9_KEY = "1920x1080-native-16x9"
RATIO_OPTIONS = [
    ("16:9 (native) - default", NATIVE_16x9_KEY),
    ("16:10 in-game (on a 16:9 monitor)", ""),
]
RES_BY_RATIO_LABEL = {label: res for label, res in RATIO_OPTIONS}


def _ratio_label_for(force_res):
    """Map a stored force_resolution to its Display-ratio label (default 16:9)."""
    return RATIO_OPTIONS[0][0] if force_res == NATIVE_16x9_KEY else RATIO_OPTIONS[1][0]

# Capture-mode picker: (menu label, (capture.mode, capture.on_demand)).
#   ("monitor", False) = continuous WGC (reliable; the v1.2.x behavior).
#   ("monitor", True)  = on-demand WGC (less fullscreen stutter, BETA - can miss end screens).
#   ("obs", False)     = read the OBS Virtual Camera (zero game overhead, needs OBS).
CAPTURE_MODES = [
    ("Continuous - reliable (default)", ("monitor", False)),
    ("On-demand - less stutter (BETA)", ("monitor", True)),
    ("OBS Virtual Camera - zero stutter (needs OBS)", ("obs", False)),
]
SETTING_BY_LABEL = {label: setting for label, setting in CAPTURE_MODES}


def _label_for_setting(mode, on_demand):
    """Map a (mode, on_demand) config pair to its picker label. OBS ignores
    on_demand; unknown combos fall back to the first (continuous) option."""
    if mode == "obs":
        return CAPTURE_MODES[2][0]
    for label, (m, od) in CAPTURE_MODES:
        if m == mode and od == bool(on_demand):
            return label
    return CAPTURE_MODES[0][0]

# Status state -> (dot color, human text)
STATE_UI = {
    "starting": (GREY, "Starting..."),
    "waiting":  (AMBER, "Waiting - start Apex / bring it on screen"),
    "watching": (GREEN, "Watching - capture OK"),
    "stale":    (AMBER, "Capture stale - is Apex visible?"),
    "stopped":  (GREY, "Stopped"),
    "idle":     (GREY, "Idle - press Start"),
}

_UNSET = object()  # distinguishes "no update result yet" from "check failed (None)"

# --------------------------------------------------------------------------- #
# "All settings" editor schema. Each field: (config path, type, label, help).
# Paths use dots for nesting (e.g. "capture.throttle_ms"). This is the curated
# set of user-tunable knobs WITH their help text; deliberately omitted are (a)
# things the main window already edits (roster/UIDs, display ratio, capture mode,
# dashboard URL) to keep one source of truth, and (b) the region-calibration
# matrices (detect/header/columns/rows/profiles) which belong to `calibrate` /
# CALIBRATION.md and would be error-prone to hand-edit here.
# --------------------------------------------------------------------------- #
SETTINGS_GROUPS = [
    ("General", [
        ("poll_seconds", "float", "Poll interval (seconds)",
         "How often the watcher checks the screen during play. 1.0 is plenty."),
        ("settle_seconds", "float", "Settle delay (seconds)",
         "Wait after the summary banner appears before reading it, so placement and "
         "stats finish drawing (prevents blank/partial rows)."),
        ("heartbeat_seconds", "int", "Heartbeat (seconds)",
         "How often the status shows a 'still watching' tick. 0 disables it."),
        ("low_priority", "bool", "Run at below-normal priority",
         "Let Windows favour Apex over the tracker. Recommended on."),
        ("name_match_cutoff", "float", "Name match cutoff (0-1)",
         "How close an OCR'd name must be to a known gamertag to snap to it. "
         "Higher = stricter."),
    ]),
    ("Ranked & RP", [
        ("ranked_detect_enabled", "bool", "Detect ranked vs pub matches",
         "Read the top-right HUD rank badge during play to stamp each match as "
         "ranked or pub. Run supabase_migration_ranked.sql before turning this on, "
         "or Supabase will reject the new column."),
        ("ranked_check_seconds", "int", "Ranked check interval (seconds)",
         "Seconds between rank-badge checks during gameplay. 20-30 is plenty; the "
         "badge is on screen the whole match."),
        ("als_assume_zero_on_timeout", "bool", "Assume 0 RP on timeout",
         "If a match's RP never moves within 5 min, record 0 instead of leaving it "
         "blank. Only safe once 'Detect ranked' is on (pubs are then skipped)."),
        ("rank_poll_seconds", "int", "ALS poll interval (seconds)",
         "How often the ALS rank tracker queries each player's RP. Needs ALS_API_KEY."),
    ]),
    ("Capture (advanced)", [
        ("capture.throttle_ms", "int", "Capture throttle (ms)",
         "Caps how often the screen grab refreshes (monitor/window modes). Higher "
         "= less fullscreen stutter, slightly slower to notice the end screen."),
        ("capture.monitor_index", "int", "Monitor index",
         "Which monitor to capture if Apex's window isn't found (1-based)."),
        ("capture.idle_probe_seconds", "int", "On-demand probe interval (seconds)",
         "On-demand mode only: how often to briefly glance for the end screen."),
        ("capture.debug_log", "bool", "Write capture debug log",
         "Append capture-probe details to debug/tracker_debug.log for troubleshooting."),
        ("capture.video_device_name", "str", "OBS camera name",
         "OBS mode: substring of the virtual-camera device name to auto-find."),
        ("capture.video_device_index", "int", "OBS camera index (-1 = auto)",
         "OBS mode: force a specific device index, or -1 to auto-detect by name."),
        ("capture.video_width", "int", "OBS capture width",
         "OBS mode: width to request from the virtual camera. Match your OBS canvas."),
        ("capture.video_height", "int", "OBS capture height",
         "OBS mode: height to request from the virtual camera."),
    ]),
    ("Output", [
        ("csv_path", "str", "CSV file path",
         "Where matches are written. A bare filename sits next to the app."),
        ("new_file_each_run", "bool", "New CSV each run",
         "Start a fresh timestamped CSV every time you press Start, instead of one "
         "running file that accumulates every match."),
    ]),
]


def _cfg_get(cfg, path):
    """Read a dotted config path (e.g. 'capture.throttle_ms'), or None if absent."""
    cur = cfg
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _cfg_set(cfg, path, value):
    """Write a dotted config path, creating intermediate dicts as needed."""
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


class App:
    def __init__(self, root):
        self.root = root
        self.cfg = core.load_config()
        self._thread = None
        self._stop = None
        self._latest = {"state": "idle", "event": "tick"}
        self._update_result = _UNSET  # set by the update-check thread (may be None)
        self._dirty = False  # unsaved settings changes

        root.title(f"Apex Tracker  v{core.__version__}")
        root.geometry("560x880")
        root.minsize(540, 760)
        self.bg = BG  # real themed bg is resolved in _apply_theme
        self._apply_theme()
        self._build()
        self._set_running(False)
        self._refresh()  # start the 500ms UI poll

    # ------------------------------------------------------------------ theme
    def _apply_theme(self):
        """Apply the Sun Valley (Windows 11-style) ttk theme. sv-ttk owns the
        widget look - rounded buttons/entries/cards, real toggle switches - so we
        only resolve the themed background (for the few classic-Tk widgets and the
        Toplevel) and add two tiny label variants. Falls back to a flat clam dark
        theme if sv-ttk isn't installed, so the app still runs."""
        style = ttk.Style()
        if sv_ttk is not None:
            sv_ttk.set_theme("dark")
            # sv-ttk doesn't expose its bg via style.lookup or set the root bg
            # (frames cover everything), so use its known sun-valley-dark base for
            # the few classic-Tk widgets (the status dot, the scrollable Toplevel).
            self.bg = "#1c1c1c"
        else:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
            style.configure(".", background=BG, foreground=TEXT,
                            fieldbackground=INPUT, bordercolor=BORDER)
            for s in ("TFrame", "TLabel", "TLabelframe", "TCheckbutton"):
                style.configure(s, background=BG, foreground=TEXT)
            self.bg = style.lookup("TFrame", "background") or BG
        self.root.configure(bg=self.bg)
        # Label variants layered on top of whatever theme is active (no bg set, so
        # they inherit the themed surface and never paint a mismatched rectangle).
        style.configure("Muted.TLabel", foreground=MUTED)
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))

    # ------------------------------------------------------------------ build
    def _build(self):
        pad = {"padx": 12, "pady": 5}

        # Header
        head = ttk.Frame(self.root)
        head.pack(fill="x", padx=12, pady=(12, 4))
        ttk.Label(head, text="Apex Damage / Kill Tracker",
                  style="Title.TLabel").pack(side="left")
        ttk.Label(head, text=f"v{core.__version__}",
                  style="Muted.TLabel").pack(side="right", pady=(8, 0))

        # Status card
        card = ttk.LabelFrame(self.root, text="STATUS")
        card.pack(fill="x", **pad)
        row = ttk.Frame(card)
        row.pack(fill="x", padx=10, pady=(8, 4))
        self.dot = tk.Canvas(row, width=14, height=14, highlightthickness=0, bg=self.bg)
        self.dot.pack(side="left", padx=(0, 9))
        self._dot_id = self.dot.create_oval(1, 1, 13, 13, fill=GREY, outline="")
        self.status_lbl = ttk.Label(row, text="Idle", font=("Segoe UI", 11, "bold"))
        self.status_lbl.pack(side="left")
        # Live badge: lights up the moment the rank badge is detected in-match, so
        # you can see a game was tagged ranked before it even ends.
        self.ranked_lbl = ttk.Label(row, text="", font=("Segoe UI", 9, "bold"))
        self.ranked_lbl.pack(side="right")
        self.detail_lbl = ttk.Label(card, text="", style="Muted.TLabel",
                                    wraplength=470, justify="left")
        self.detail_lbl.pack(anchor="w", padx=10)
        self.last_lbl = ttk.Label(card, text="No matches logged yet this run.",
                                  foreground=TEXT, wraplength=470, justify="left")
        self.last_lbl.pack(anchor="w", padx=10, pady=(3, 10))

        # Controls - primary actions on top, secondary tools beneath, so the row
        # never overflows the window width.
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctrl, text="Start", command=self.start,
                                    style="Accent.TButton")
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctrl, text="Stop", command=self.stop)
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(ctrl, text="All settings…",
                   command=self.open_advanced_settings).pack(side="right")
        ttk.Button(ctrl, text="Dashboard",
                   command=self.open_dashboard).pack(side="right", padx=6)

        tools = ttk.Frame(self.root)
        tools.pack(fill="x", padx=12)
        ttk.Button(tools, text="Open CSV", command=self.open_csv).pack(side="left")
        self.shot_btn = ttk.Button(tools, text="Capture frame", command=self.capture_frame)
        self.shot_btn.pack(side="left", padx=6)

        # Settings
        sett = ttk.LabelFrame(self.root, text="QUICK SETTINGS")
        sett.pack(fill="both", expand=True, **pad)

        ttk.Label(sett, text="Squad roster",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
        ttk.Label(sett, style="Muted.TLabel", wraplength=470, justify="left",
                  text="Gamertags the OCR snaps names to. Add an optional ALS UID to map "
                       "a player to their account when name lookup fails (Steam: your "
                       "17-digit SteamID64, from your apexlegendsstatus.com profile URL).").pack(
            anchor="w", padx=10, pady=(0, 4))

        rosterf = ttk.Frame(sett)
        rosterf.pack(fill="both", expand=True, padx=10, pady=2)
        self.roster = ttk.Treeview(rosterf, columns=("name", "uid"),
                                   show="headings", height=5, selectmode="browse")
        self.roster.heading("name", text="Gamertag")
        self.roster.heading("uid", text="ALS UID (optional)")
        self.roster.column("name", width=170, anchor="w")
        self.roster.column("uid", width=200, anchor="w")
        self.roster.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(rosterf, orient="vertical", command=self.roster.yview)
        sb.pack(side="left", fill="y")
        self.roster.config(yscrollcommand=sb.set)
        self.roster.bind("<<TreeviewSelect>>", self._on_roster_select)
        uids = self.cfg.get("als_uids", {}) or {}
        for n in self.cfg.get("known_names", []):
            self.roster.insert("", "end", values=(n, uids.get(n, "")))
        rbtns = ttk.Frame(rosterf)
        rbtns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(rbtns, text="Remove", command=self._roster_remove).pack(fill="x")

        # Add / edit form: gamertag + optional UID.
        addf = ttk.Frame(sett)
        addf.pack(fill="x", padx=10, pady=(4, 0))
        self.name_var = tk.StringVar()
        self.uid_var = tk.StringVar()
        ttk.Label(addf, text="Gamertag").grid(row=0, column=0, sticky="w")
        ttk.Label(addf, text="ALS UID").grid(row=0, column=1, sticky="w", padx=(6, 0))
        name_entry = ttk.Entry(addf, textvariable=self.name_var)
        name_entry.grid(row=1, column=0, sticky="ew")
        uid_entry = ttk.Entry(addf, textvariable=self.uid_var, width=20)
        uid_entry.grid(row=1, column=1, sticky="ew", padx=(6, 6))
        ttk.Button(addf, text="Add / Update", command=self._roster_add).grid(row=1, column=2)
        addf.columnconfigure(0, weight=3)
        addf.columnconfigure(1, weight=2)
        name_entry.bind("<Return>", lambda e: self._roster_add())
        uid_entry.bind("<Return>", lambda e: self._roster_add())

        resf = ttk.Frame(sett)
        resf.pack(fill="x", padx=10, pady=(10, 0))
        ttk.Label(resf, text="Display ratio:").pack(side="left")
        self.ratio_var = tk.StringVar(
            value=_ratio_label_for(self.cfg.get("force_resolution") or ""))
        self.ratio_combo = ttk.Combobox(resf, textvariable=self.ratio_var,
                                        values=[lbl for lbl, _ in RATIO_OPTIONS],
                                        state="readonly", width=34)
        self.ratio_combo.pack(side="left", padx=6)
        self.ratio_combo.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        ttk.Label(sett, wraplength=470, justify="left", style="Muted.TLabel",
                  text="Resolution is auto-detected and scales to any 16:9 res (1080p/1440p/4K). "
                       "Pick 16:10 only if you run a 16:10 aspect IN-GAME on a 16:9 monitor. "
                       "True 16:10 monitors and ultrawide (21:9) aren't calibrated yet.").pack(
            anchor="w", padx=10, pady=(2, 0))

        # Capture mode picker (continuous / on-demand / OBS).
        capf = ttk.Frame(sett)
        capf.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(capf, text="Capture mode:").pack(side="left")
        cap_cfg = self.cfg.get("capture") or {}
        self.mode_var = tk.StringVar(
            value=_label_for_setting(cap_cfg.get("mode", "monitor"),
                                     cap_cfg.get("on_demand", False)))
        self.mode_combo = ttk.Combobox(capf, textvariable=self.mode_var,
                                       values=[lbl for lbl, _ in CAPTURE_MODES],
                                       state="readonly", width=40)
        self.mode_combo.pack(side="left", padx=6)
        self.mode_combo.bind("<<ComboboxSelected>>",
                             lambda e: (self._on_capture_mode_change(), self._mark_dirty()))
        self.mode_note = ttk.Label(sett, text="", style="Muted.TLabel", wraplength=470,
                                   justify="left")
        self.mode_note.pack(anchor="w", padx=10)
        self._on_capture_mode_change()  # set the initial hint (not a user change)

        dashf = ttk.Frame(sett)
        dashf.pack(fill="x", padx=10, pady=(8, 2))
        ttk.Label(dashf, text="Dashboard URL:").pack(side="left")
        self.dash_var = tk.StringVar(value=self.cfg.get("dashboard_url", ""))
        dash_entry = ttk.Entry(dashf, textvariable=self.dash_var)
        dash_entry.pack(side="left", fill="x", expand=True, padx=6)
        dash_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())

        # ALS API key (free, per-user) - enables Rank/RP tracking on this machine.
        alsf = ttk.Frame(sett)
        alsf.pack(fill="x", padx=10, pady=(8, 0))
        ttk.Label(alsf, text="ALS API key:").pack(side="left")
        self.als_key_var = tk.StringVar(value=self.cfg.get("als_api_key", ""))
        als_entry = ttk.Entry(alsf, textvariable=self.als_key_var, show="•")
        als_entry.pack(side="left", fill="x", expand=True, padx=6)
        als_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())
        ttk.Label(sett, wraplength=470, justify="left", style="Muted.TLabel",
                  text="Free key for Rank/RP tracking: apexlegendsstatus.com -> sign in -> "
                       "Settings -> API. Each user uses their own. Stored locally in "
                       "config.json; applied on Start (Stop then Start if already running).").pack(
            anchor="w", padx=10, pady=(2, 0))

        savef = ttk.Frame(sett)
        savef.pack(fill="x", padx=10, pady=(6, 10))
        self.save_btn = ttk.Button(savef, text="Save settings", command=self.save_settings)
        self.save_btn.pack(side="left")
        self.save_note = ttk.Label(savef, text="Settings are applied on Start.",
                                   style="Muted.TLabel")
        self.save_note.pack(side="left", padx=8)

        # Footer: update check
        foot = ttk.Frame(self.root)
        foot.pack(fill="x", **pad)
        self.update_btn = ttk.Button(foot, text="Check for updates", command=self.check_updates)
        self.update_btn.pack(side="left")
        self.update_lbl = ttk.Label(foot, text="", style="Muted.TLabel")
        self.update_lbl.pack(side="left", padx=8)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------- unsaved state
    def _mark_dirty(self):
        """Flag that a setting changed but isn't saved yet, and make that obvious:
        a red note + an asterisk on the Save button. Cleared by save_settings."""
        self._dirty = True
        self.save_btn.config(text="Save settings *")
        self.save_note.config(text="Unsaved - click Save settings to apply",
                              foreground=RED)

    # ------------------------------------------------------------- roster ops
    def _on_roster_select(self, *_):
        """Load the selected row into the edit fields so its UID can be added/changed."""
        sel = self.roster.selection()
        if not sel:
            return
        name, uid = self.roster.item(sel[0], "values")
        self.name_var.set(name)
        self.uid_var.set(uid)

    def _roster_add(self):
        """Add a new gamertag, or update an existing one's UID (upsert by name)."""
        name = self.name_var.get().strip()
        uid = self.uid_var.get().strip()
        if not name:
            return
        for iid in self.roster.get_children():
            if self.roster.set(iid, "name") == name:
                self.roster.item(iid, values=(name, uid))  # update existing
                break
        else:
            self.roster.insert("", "end", values=(name, uid))
        self._mark_dirty()
        self.name_var.set("")
        self.uid_var.set("")

    def _roster_remove(self):
        sel = self.roster.selection()
        if not sel:
            return
        for iid in sel:
            self.roster.delete(iid)
        self._mark_dirty()
        self.name_var.set("")
        self.uid_var.set("")

    # --------------------------------------------------------- capture mode
    def _on_capture_mode_change(self, *_):
        """Update the hint under the picker; for OBS mode, check live whether the
        OBS Virtual Camera is actually present right now."""
        mode, on_demand = SETTING_BY_LABEL.get(self.mode_var.get(), ("monitor", False))
        if mode == "obs":
            name = (self.cfg.get("capture") or {}).get("video_device_name", "OBS Virtual")
            try:
                idx = core.find_video_device_index(name)
            except Exception:
                idx = None
            if idx is None:
                self.mode_note.config(
                    text="Needs OBS running with a Game Capture of Apex + "
                         "'Start Virtual Camera'. Camera not detected right now.",
                    foreground=RED)
            else:
                self.mode_note.config(
                    text=f"OBS Virtual Camera detected (device {idx}). Zero game "
                         f"overhead while OBS is running.",
                    foreground=GREEN)
        elif on_demand:
            self.mode_note.config(
                text="BETA: captures in bursts to reduce fullscreen stutter, but can "
                     "MISS a match's end screen and not log it. Use OBS for stutter-free "
                     "+ reliable.",
                foreground=AMBER)
        else:
            self.mode_note.config(
                text="Reliable default. Captures continuously; may cause some stutter in "
                     "exclusive fullscreen (use Borderless or OBS to avoid it).",
                foreground=MUTED)

    # ------------------------------------------------------------- watcher
    def _set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        # Catch the easy-to-miss case: changed a setting (resolution / capture mode)
        # but never clicked Save, so it wouldn't take effect this run.
        if self._dirty:
            ans = messagebox.askyesnocancel(
                "Unsaved settings",
                "You changed settings but haven't saved them, so they won't apply.\n\n"
                "Save them now and start?")
            if ans is None:        # Cancel - don't start
                return
            if ans:                # Yes - save first
                self.save_settings()
        self.cfg = core.load_config()  # pick up any saved settings
        self._stop = threading.Event()
        forced = self.cfg.get("force_resolution") or None

        def worker():
            try:
                core.run_watch(self.cfg, forced, stop_event=self._stop,
                               status_cb=self._on_status)
            except Exception as e:  # surface, don't die silently
                self._latest = {"state": "stopped", "event": "error", "error": str(e)}

        self._thread = threading.Thread(target=worker, daemon=True)
        self._thread.start()
        self._set_running(True)

    def stop(self):
        if self._stop:
            self._stop.set()
        self._set_running(False)

    def _on_status(self, s):
        # Called on the watcher thread - just stash it; the UI poll reads it.
        self._latest = s

    # ------------------------------------------------------------- UI poll
    def _refresh(self):
        s = self._latest
        state = s.get("state", "idle")
        color, text = STATE_UI.get(state, (GREY, state))
        if s.get("event") == "error":
            color, text = RED, "Error - see details"
        self.dot.itemconfig(self._dot_id, fill=color)
        self.status_lbl.config(text=text)

        bits = []
        if s.get("src"):
            bits.append(str(s["src"]))
        if s.get("resolution"):
            prof = s.get("profile")
            bits.append(f"{s['resolution']} ({'profile ' + prof if prof else 'auto-scaled'})")
        if s.get("logged_this_run") is not None and self.stop_btn["state"] != "disabled":
            bits.append(f"{s['logged_this_run']} logged this run")
        if s.get("error"):
            bits.append(s["error"])
        self.detail_lbl.config(text="  |  ".join(bits))

        # Live ranked badge: lit while the current match is flagged ranked (the HUD
        # badge was detected), cleared once that match is logged / a new one starts.
        watching = self.stop_btn["state"] != "disabled"
        if watching and s.get("match_ranked"):
            self.ranked_lbl.config(text="● RANKED", foreground=GREEN)
        else:
            self.ranked_lbl.config(text="")

        lm = s.get("last_match")
        if lm:
            who = ", ".join(f"{p['name']} {p['kills']}k/{p['damage']}" for p in lm["players"])
            tag = {True: "RANKED", False: "PUB"}.get(lm.get("ranked"))
            tagtxt = f"  ·  {tag}" if tag else ""
            self.last_lbl.config(
                text=f"Last [{lm['time']}]{tagtxt}  #{lm['placed']}  "
                     f"{lm['total_kills']} squad kills\n{who}")

        if self._update_result is not _UNSET:
            self._show_update_result(self._update_result)
            self._update_result = _UNSET

        self.root.after(500, self._refresh)

    # ------------------------------------------------------------- settings
    def save_settings(self):
        self.cfg = core.load_config()
        names, uids = [], {}
        for iid in self.roster.get_children():
            nm = self.roster.set(iid, "name").strip()
            ud = self.roster.set(iid, "uid").strip()
            if not nm:
                continue
            names.append(nm)
            if ud:
                uids[nm] = ud
        self.cfg["known_names"] = names
        self.cfg["als_uids"] = uids
        self.cfg["force_resolution"] = RES_BY_RATIO_LABEL.get(self.ratio_var.get(), "")
        self.cfg["dashboard_url"] = self.dash_var.get().strip()
        self.cfg["als_api_key"] = self.als_key_var.get().strip()
        mode, on_demand = SETTING_BY_LABEL.get(self.mode_var.get(), ("monitor", False))
        cap = self.cfg.setdefault("capture", {})
        cap["mode"] = mode
        cap["on_demand"] = on_demand
        core.save_config(self.cfg)
        self._dirty = False
        self.save_btn.config(text="Save settings")
        running = bool(self._thread and self._thread.is_alive())
        self.save_note.config(
            text="Saved - Stop then Start to apply." if running else "Saved.",
            foreground=GREEN)
        self.root.after(5000, lambda: (
            self.save_note.config(text="Settings are applied on Start.",
                                  foreground=MUTED) if not self._dirty else None))

    # ------------------------------------------------- all-settings editor
    def open_advanced_settings(self):
        """A scrollable, schema-driven editor for every tunable config key (see
        SETTINGS_GROUPS), each with its help text. Reads config fresh, writes only
        the edited keys back, and leaves the calibration matrices + roster/ratio/
        capture-mode (edited in the main window) untouched."""
        cfg = core.load_config()
        win = tk.Toplevel(self.root)
        win.title("All settings")
        win.configure(bg=self.bg)
        win.geometry("560x640")
        win.minsize(520, 480)
        win.transient(self.root)

        # Scrollable body: a themed Canvas hosting an inner Frame.
        body = ttk.Frame(win)
        body.pack(fill="both", expand=True)
        canvas = tk.Canvas(body, bg=self.bg, highlightthickness=0)
        canvas.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
        vsb.pack(side="right", fill="y")
        canvas.configure(yscrollcommand=vsb.set)
        inner = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfigure(win_id, width=e.width))

        def _on_wheel(e):
            canvas.yview_scroll(int(-e.delta / 120), "units")
        canvas.bind_all("<MouseWheel>", _on_wheel)
        # Unbind the global wheel handler when this dialog closes.
        win.bind("<Destroy>", lambda e: canvas.unbind_all("<MouseWheel>")
                 if e.widget is win else None)

        ttk.Label(inner, text="All settings", style="Title.TLabel").pack(
            anchor="w", padx=16, pady=(14, 2))
        ttk.Label(inner, style="Muted.TLabel", wraplength=500, justify="left",
                  text="Every tunable option lives here. Roster, display ratio and "
                       "capture mode are on the main window; screen-region calibration "
                       "is done with the 'calibrate' command (see CALIBRATION.md).").pack(
            anchor="w", padx=16, pady=(0, 8))

        fields = []  # (path, type, StringVar/BooleanVar)
        for group, entries in SETTINGS_GROUPS:
            sec = ttk.LabelFrame(inner, text=group.upper())
            sec.pack(fill="x", padx=14, pady=6)
            for path, typ, label, helptext in entries:
                val = _cfg_get(cfg, path)
                rowf = ttk.Frame(sec)
                rowf.pack(fill="x", padx=10, pady=(6, 0))
                if typ == "bool":
                    var = tk.BooleanVar(value=bool(val))
                    # sv-ttk renders this as a modern toggle switch.
                    sw_style = "Switch.TCheckbutton" if sv_ttk else "TCheckbutton"
                    ttk.Checkbutton(rowf, text=label, variable=var,
                                    style=sw_style).pack(side="left")
                    fields.append((path, typ, var))
                else:
                    ttk.Label(rowf, text=label, font=("Segoe UI", 10, "bold")).pack(
                        side="left")
                    var = tk.StringVar(value="" if val is None else str(val))
                    ttk.Entry(rowf, textvariable=var, width=16).pack(side="right")
                    fields.append((path, typ, var))
                ttk.Label(sec, text=helptext, style="Muted.TLabel",
                          wraplength=500, justify="left").pack(
                    anchor="w", padx=10, pady=(1, 2))

        # Sticky action bar at the bottom of the window (outside the scroll area).
        bar = ttk.Frame(win)
        bar.pack(fill="x", side="bottom", padx=14, pady=10)
        note = ttk.Label(bar, text="", style="Muted.TLabel")
        note.pack(side="left")

        def _save():
            new = core.load_config()  # fresh, so we never clobber unrelated edits
            for path, typ, var in fields:
                if typ == "bool":
                    _cfg_set(new, path, bool(var.get()))
                elif typ in ("int", "float"):
                    raw = var.get().strip()
                    if raw == "":
                        continue  # blank = leave the existing value as-is
                    try:
                        _cfg_set(new, path, int(raw) if typ == "int" else float(raw))
                    except ValueError:
                        messagebox.showerror(
                            "Invalid value",
                            f"'{path}' needs a {'whole number' if typ == 'int' else 'number'}, "
                            f"got '{raw}'.", parent=win)
                        return
                else:
                    _cfg_set(new, path, var.get().strip())
            core.save_config(new)
            self.cfg = new
            running = bool(self._thread and self._thread.is_alive())
            note.config(text="Saved - Stop then Start to apply." if running else "Saved.",
                        foreground=GREEN)
            win.after(900, win.destroy)

        ttk.Button(bar, text="Cancel", command=win.destroy).pack(side="right")
        ttk.Button(bar, text="Save", command=_save,
                   style="Accent.TButton").pack(side="right", padx=6)

        win.grab_set()

    # ------------------------------------------------------------- actions
    def open_csv(self):
        path = core.csv_path(core.load_config())
        if os.path.exists(path):
            os.startfile(path)  # noqa: SLF / Windows only, which is the target
        else:
            messagebox.showinfo("No CSV yet",
                                "No matches have been logged yet, so there's no CSV.")

    def capture_frame(self):
        """Grab one frame via the current capture mode and save it to debug/, so the
        user can send it for calibration/diagnostics without using the console.
        Each grab is timestamped so repeated clicks never overwrite an earlier one.
        Runs off the UI thread (capture can take a few seconds)."""
        self.shot_btn.config(state="disabled")
        self.save_note.config(text="Capturing a frame...", foreground=MUTED)
        cfg = core.load_config()

        def worker():
            res = {}
            try:
                cap, src, _ = core.open_live_capture(cfg)
                ok = cap.wait_first(8)
                frame = cap.grab() if ok else None
                if frame is None:
                    res["error"] = (f"No frame received from {src}.\n\n"
                                    "Make sure Apex (or OBS, in OBS mode) is running "
                                    "and visible on screen, then try again.")
                else:
                    import cv2 as _cv2
                    os.makedirs(core.DEBUG_DIR, exist_ok=True)
                    # Timestamped name so clicking Capture again keeps the prior shots.
                    fname = f"capture_{datetime.now():%Y%m%d_%H%M%S}.png"
                    out = os.path.join(core.DEBUG_DIR, fname)
                    _cv2.imwrite(out, frame)
                    res.update(path=out, w=frame.shape[1], h=frame.shape[0],
                               mb=float(frame.mean()), src=src)
                cap.release()
            except Exception as e:
                res["error"] = str(e)
            self.root.after(0, lambda: self._capture_frame_done(res))

        threading.Thread(target=worker, daemon=True).start()

    def _capture_frame_done(self, res):
        self.shot_btn.config(state="normal")
        self.save_note.config(text="")
        if res.get("error"):
            messagebox.showerror("Capture failed", res["error"])
            return
        black = res["mb"] < 2.0
        msg = (f"Saved a {res['w']}x{res['h']} frame from {res['src']} "
               f"(brightness {res['mb']:.0f}).\n\n")
        if black:
            msg += ("WARNING: the frame looks BLACK - the capture was blocked or the "
                    "source isn't visible. Bring Apex/OBS on screen and try again.\n\n")
        msg += f"File:\n{res['path']}\n\nOpen the debug folder to grab it?"
        if messagebox.askyesno("Frame captured", msg):
            try:
                os.startfile(os.path.dirname(res["path"]))
            except Exception:
                pass

    def open_dashboard(self):
        url = self.dash_var.get().strip() or self.cfg.get("dashboard_url", "")
        if url:
            webbrowser.open(url)
        else:
            messagebox.showinfo(
                "No dashboard set",
                "Paste your stats dashboard URL into Settings -> Dashboard URL, "
                "then Save settings.")

    def check_updates(self):
        self.update_lbl.config(text="Checking...")
        self.update_btn.config(state="disabled")

        def worker():
            self._update_result = core.latest_release_version()

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_result(self, tag):
        self.update_btn.config(state="normal")
        if not tag:
            self.update_lbl.config(
                text="Couldn't reach GitHub - try again later.", foreground=RED)
            return
        latest = tag.lstrip("v")
        if latest == core.__version__:
            self.update_lbl.config(
                text=f"Up to date (v{core.__version__}).", foreground=GREEN)
        else:
            self.update_lbl.config(
                text=f"Update available: {tag}", foreground=RED)
            if messagebox.askyesno(
                    "Update available",
                    f"You have v{core.__version__}; {tag} is available.\n\nOpen the "
                    f"download page?"):
                webbrowser.open(f"https://github.com/{core.REPO}/releases/latest")

    def _on_close(self):
        if self._stop:
            self._stop.set()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
