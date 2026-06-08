"""
Apex Tracker - desktop UI.

A small Tkinter front-end over apex_tracker.run_watch: Start/Stop the passive
watcher, see a live status/heartbeat, edit the squad roster + resolution without
touching config.json, check for updates, and open the stats dashboard. It stays
strictly passive - it only drives the existing screen-reader; it never touches the
game.

Run from source:  py apex_gui.py
Frozen:           ApexTrackerUI.exe
"""

import os
import sys
import threading
import webbrowser

# A windowed (--noconsole) PyInstaller build has no stdout/stderr; library print()
# calls would then crash. Point them at a sink before anything else runs.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import tkinter as tk
from tkinter import ttk, messagebox

import apex_tracker as core

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
    "starting": ("#888888", "Starting..."),
    "waiting":  ("#e0a020", "Waiting - start Apex / bring it on screen"),
    "watching": ("#2ea043", "Watching - capture OK"),
    "stale":    ("#e0a020", "Capture stale - is Apex visible?"),
    "stopped":  ("#888888", "Stopped"),
    "idle":     ("#888888", "Idle - press Start"),
}

_UNSET = object()  # distinguishes "no update result yet" from "check failed (None)"


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
        root.geometry("460x640")
        root.minsize(440, 600)
        self._build()
        self._set_running(False)
        self._refresh()  # start the 500ms UI poll

    # ------------------------------------------------------------------ build
    def _build(self):
        pad = {"padx": 10, "pady": 4}
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        # Header
        head = ttk.Frame(self.root)
        head.pack(fill="x", **pad)
        ttk.Label(head, text="Apex Damage / Kill Tracker",
                  font=("Segoe UI", 13, "bold")).pack(side="left")
        ttk.Label(head, text=f"v{core.__version__}",
                  foreground="#888").pack(side="right")

        # Status card
        card = ttk.LabelFrame(self.root, text="Status")
        card.pack(fill="x", **pad)
        row = ttk.Frame(card)
        row.pack(fill="x", padx=8, pady=6)
        self.dot = tk.Canvas(row, width=16, height=16, highlightthickness=0)
        self.dot.pack(side="left", padx=(0, 8))
        self._dot_id = self.dot.create_oval(2, 2, 14, 14, fill="#888888", outline="")
        self.status_lbl = ttk.Label(row, text="Idle", font=("Segoe UI", 10, "bold"))
        self.status_lbl.pack(side="left")
        self.detail_lbl = ttk.Label(card, text="", foreground="#555")
        self.detail_lbl.pack(anchor="w", padx=8)
        self.last_lbl = ttk.Label(card, text="No matches logged yet this run.",
                                  foreground="#333", wraplength=400, justify="left")
        self.last_lbl.pack(anchor="w", padx=8, pady=(2, 8))

        # Controls
        ctrl = ttk.Frame(self.root)
        ctrl.pack(fill="x", **pad)
        self.start_btn = ttk.Button(ctrl, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(ctrl, text="Stop", command=self.stop)
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(ctrl, text="Open CSV", command=self.open_csv).pack(side="left")
        self.shot_btn = ttk.Button(ctrl, text="Capture frame", command=self.capture_frame)
        self.shot_btn.pack(side="left", padx=6)
        ttk.Button(ctrl, text="Dashboard", command=self.open_dashboard).pack(side="right")

        # Settings
        sett = ttk.LabelFrame(self.root, text="Settings")
        sett.pack(fill="both", expand=True, **pad)

        ttk.Label(sett, text="Squad roster (gamertags the OCR snaps names to):").pack(
            anchor="w", padx=8, pady=(6, 0))
        rosterf = ttk.Frame(sett)
        rosterf.pack(fill="both", expand=True, padx=8, pady=4)
        self.roster = tk.Listbox(rosterf, height=6)
        self.roster.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(rosterf, orient="vertical", command=self.roster.yview)
        sb.pack(side="left", fill="y")
        self.roster.config(yscrollcommand=sb.set)
        for n in self.cfg.get("known_names", []):
            self.roster.insert("end", n)
        rbtns = ttk.Frame(rosterf)
        rbtns.pack(side="left", fill="y", padx=(6, 0))
        ttk.Button(rbtns, text="Remove", command=self._roster_remove).pack(fill="x")
        addf = ttk.Frame(sett)
        addf.pack(fill="x", padx=8)
        self.add_entry = ttk.Entry(addf)
        self.add_entry.pack(side="left", fill="x", expand=True)
        self.add_entry.bind("<Return>", lambda e: self._roster_add())
        ttk.Button(addf, text="Add name", command=self._roster_add).pack(side="left", padx=6)

        resf = ttk.Frame(sett)
        resf.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(resf, text="Display ratio:").pack(side="left")
        self.ratio_var = tk.StringVar(
            value=_ratio_label_for(self.cfg.get("force_resolution") or ""))
        self.ratio_combo = ttk.Combobox(resf, textvariable=self.ratio_var,
                                        values=[lbl for lbl, _ in RATIO_OPTIONS],
                                        state="readonly", width=34)
        self.ratio_combo.pack(side="left", padx=6)
        self.ratio_combo.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        ttk.Label(sett, wraplength=420, justify="left", foreground="#888",
                  text="Resolution is auto-detected and scales to any 16:9 res (1080p/1440p/4K). "
                       "Pick 16:10 only if you run a 16:10 aspect IN-GAME on a 16:9 monitor. "
                       "True 16:10 monitors and ultrawide (21:9) aren't calibrated yet.").pack(
            anchor="w", padx=8, pady=(2, 0))

        # Capture mode picker (continuous / on-demand / OBS).
        capf = ttk.Frame(sett)
        capf.pack(fill="x", padx=8, pady=(8, 0))
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
        self.mode_note = ttk.Label(sett, text="", foreground="#555", wraplength=400,
                                   justify="left")
        self.mode_note.pack(anchor="w", padx=8)
        self._on_capture_mode_change()  # set the initial hint (not a user change)

        dashf = ttk.Frame(sett)
        dashf.pack(fill="x", padx=8, pady=2)
        ttk.Label(dashf, text="Dashboard URL:").pack(side="left")
        self.dash_var = tk.StringVar(value=self.cfg.get("dashboard_url", ""))
        dash_entry = ttk.Entry(dashf, textvariable=self.dash_var)
        dash_entry.pack(side="left", fill="x", expand=True, padx=6)
        dash_entry.bind("<KeyRelease>", lambda e: self._mark_dirty())

        savef = ttk.Frame(sett)
        savef.pack(fill="x", padx=8, pady=(6, 8))
        self.save_btn = ttk.Button(savef, text="Save settings", command=self.save_settings)
        self.save_btn.pack(side="left")
        self.save_note = ttk.Label(savef, text="Settings are applied on Start.",
                                   foreground="#888")
        self.save_note.pack(side="left", padx=8)

        # Footer: update check
        foot = ttk.Frame(self.root)
        foot.pack(fill="x", **pad)
        self.update_btn = ttk.Button(foot, text="Check for updates", command=self.check_updates)
        self.update_btn.pack(side="left")
        self.update_lbl = ttk.Label(foot, text="", foreground="#555")
        self.update_lbl.pack(side="left", padx=8)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # --------------------------------------------------------- unsaved state
    def _mark_dirty(self):
        """Flag that a setting changed but isn't saved yet, and make that obvious:
        a red note + an asterisk on the Save button. Cleared by save_settings."""
        self._dirty = True
        self.save_btn.config(text="Save settings *")
        self.save_note.config(text="Unsaved - click Save settings to apply",
                              foreground="#d1242f")

    # ------------------------------------------------------------- roster ops
    def _roster_add(self):
        name = self.add_entry.get().strip()
        if name and name not in self.roster.get(0, "end"):
            self.roster.insert("end", name)
            self._mark_dirty()
        self.add_entry.delete(0, "end")

    def _roster_remove(self):
        if self.roster.curselection():
            self._mark_dirty()
        for i in reversed(self.roster.curselection()):
            self.roster.delete(i)

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
                    foreground="#d1242f")
            else:
                self.mode_note.config(
                    text=f"OBS Virtual Camera detected (device {idx}). Zero game "
                         f"overhead while OBS is running.",
                    foreground="#2ea043")
        elif on_demand:
            self.mode_note.config(
                text="BETA: captures in bursts to reduce fullscreen stutter, but can "
                     "MISS a match's end screen and not log it. Use OBS for stutter-free "
                     "+ reliable.",
                foreground="#b5860b")
        else:
            self.mode_note.config(
                text="Reliable default. Captures continuously; may cause some stutter in "
                     "exclusive fullscreen (use Borderless or OBS to avoid it).",
                foreground="#555")

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
        color, text = STATE_UI.get(state, ("#888888", state))
        if s.get("event") == "error":
            color, text = "#d1242f", "Error - see details"
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

        lm = s.get("last_match")
        if lm:
            who = ", ".join(f"{p['name']} {p['kills']}k/{p['damage']}" for p in lm["players"])
            self.last_lbl.config(
                text=f"Last [{lm['time']}]  #{lm['placed']}  {lm['total_kills']} squad kills\n{who}")

        if self._update_result is not _UNSET:
            self._show_update_result(self._update_result)
            self._update_result = _UNSET

        self.root.after(500, self._refresh)

    # ------------------------------------------------------------- settings
    def save_settings(self):
        self.cfg = core.load_config()
        self.cfg["known_names"] = list(self.roster.get(0, "end"))
        self.cfg["force_resolution"] = RES_BY_RATIO_LABEL.get(self.ratio_var.get(), "")
        self.cfg["dashboard_url"] = self.dash_var.get().strip()
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
            foreground="#2ea043")
        self.root.after(5000, lambda: (
            self.save_note.config(text="Settings are applied on Start.",
                                  foreground="#888") if not self._dirty else None))

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
        Runs off the UI thread (capture can take a few seconds)."""
        self.shot_btn.config(state="disabled")
        self.save_note.config(text="Capturing a frame...", foreground="#555")
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
                    out = os.path.join(core.DEBUG_DIR, "capture_live.png")
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
                text="Couldn't reach GitHub - try again later.", foreground="#d1242f")
            return
        latest = tag.lstrip("v")
        if latest == core.__version__:
            self.update_lbl.config(
                text=f"Up to date (v{core.__version__}).", foreground="#2ea043")
        else:
            self.update_lbl.config(
                text=f"Update available: {tag}", foreground="#d1242f")
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
