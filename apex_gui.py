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

COMMON_RES = ["1920x1080", "2560x1440", "3440x1440", "3840x2160"]
AUTO_LABEL = "Auto-detect"

# Capture-mode picker: (menu label, config capture.mode value). "monitor" runs the
# on-demand WGC path (standalone, brief periodic blip); "obs" reads the OBS Virtual
# Camera (zero game overhead but needs OBS running).
CAPTURE_MODES = [
    ("Standalone - no OBS (brief blip)", "monitor"),
    ("OBS Virtual Camera - smooth (needs OBS)", "obs"),
]
MODE_BY_LABEL = {label: mode for label, mode in CAPTURE_MODES}
LABEL_BY_MODE = {mode: label for label, mode in CAPTURE_MODES}

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
        resf.pack(fill="x", padx=8, pady=(8, 2))
        ttk.Label(resf, text="Resolution:").pack(side="left")
        cur = self.cfg.get("force_resolution") or ""
        values = [AUTO_LABEL] + sorted(set(COMMON_RES + list((self.cfg.get("profiles") or {}).keys())))
        self.res_var = tk.StringVar(value=(cur if cur else AUTO_LABEL))
        self.res_combo = ttk.Combobox(resf, textvariable=self.res_var, values=values,
                                      state="readonly", width=16)
        self.res_combo.pack(side="left", padx=6)

        # Capture mode picker (standalone vs OBS Virtual Camera).
        capf = ttk.Frame(sett)
        capf.pack(fill="x", padx=8, pady=(8, 0))
        ttk.Label(capf, text="Capture mode:").pack(side="left")
        cur_mode = (self.cfg.get("capture") or {}).get("mode", "monitor")
        self.mode_var = tk.StringVar(value=LABEL_BY_MODE.get(cur_mode, CAPTURE_MODES[0][0]))
        self.mode_combo = ttk.Combobox(capf, textvariable=self.mode_var,
                                       values=[lbl for lbl, _ in CAPTURE_MODES],
                                       state="readonly", width=32)
        self.mode_combo.pack(side="left", padx=6)
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_capture_mode_change)
        self.mode_note = ttk.Label(sett, text="", foreground="#555", wraplength=400,
                                   justify="left")
        self.mode_note.pack(anchor="w", padx=8)
        self._on_capture_mode_change()  # set the initial hint

        dashf = ttk.Frame(sett)
        dashf.pack(fill="x", padx=8, pady=2)
        ttk.Label(dashf, text="Dashboard URL:").pack(side="left")
        self.dash_var = tk.StringVar(value=self.cfg.get("dashboard_url", ""))
        ttk.Entry(dashf, textvariable=self.dash_var).pack(side="left", fill="x",
                                                          expand=True, padx=6)

        savef = ttk.Frame(sett)
        savef.pack(fill="x", padx=8, pady=(6, 8))
        ttk.Button(savef, text="Save settings", command=self.save_settings).pack(side="left")
        self.save_note = ttk.Label(savef, text="", foreground="#2ea043")
        self.save_note.pack(side="left", padx=8)

        # Footer: update check
        foot = ttk.Frame(self.root)
        foot.pack(fill="x", **pad)
        self.update_btn = ttk.Button(foot, text="Check for updates", command=self.check_updates)
        self.update_btn.pack(side="left")
        self.update_lbl = ttk.Label(foot, text="", foreground="#555")
        self.update_lbl.pack(side="left", padx=8)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------- roster ops
    def _roster_add(self):
        name = self.add_entry.get().strip()
        if name and name not in self.roster.get(0, "end"):
            self.roster.insert("end", name)
        self.add_entry.delete(0, "end")

    def _roster_remove(self):
        for i in reversed(self.roster.curselection()):
            self.roster.delete(i)

    # --------------------------------------------------------- capture mode
    def _on_capture_mode_change(self, *_):
        """Update the hint under the picker and, for OBS mode, check live whether
        the OBS Virtual Camera is actually present right now."""
        mode = MODE_BY_LABEL.get(self.mode_var.get(), "monitor")
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
        else:
            self.mode_note.config(
                text="Standalone - no OBS needed. Expect a brief blip every ~12s "
                     "(exclusive fullscreen).",
                foreground="#555")

    # ------------------------------------------------------------- watcher
    def _set_running(self, running):
        self.start_btn.config(state="disabled" if running else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")

    def start(self):
        if self._thread and self._thread.is_alive():
            return
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
        sel = self.res_var.get()
        self.cfg["force_resolution"] = "" if sel == AUTO_LABEL else sel
        self.cfg["dashboard_url"] = self.dash_var.get().strip()
        self.cfg.setdefault("capture", {})["mode"] = MODE_BY_LABEL.get(
            self.mode_var.get(), "monitor")
        core.save_config(self.cfg)
        running = bool(self._thread and self._thread.is_alive())
        self.save_note.config(
            text="Saved. Restart the watcher to apply." if running else "Saved.")
        self.root.after(4000, lambda: self.save_note.config(text=""))

    # ------------------------------------------------------------- actions
    def open_csv(self):
        path = core.csv_path(core.load_config())
        if os.path.exists(path):
            os.startfile(path)  # noqa: SLF / Windows only, which is the target
        else:
            messagebox.showinfo("No CSV yet",
                                "No matches have been logged yet, so there's no CSV.")

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
