# Apex Damage / Kill Tracker

Passive, read-only screen reader. It watches your screen, detects the post-game
**CHAMPIONS / SUMMARY** screen, OCR-reads each player card + the match session id,
and appends rows to `apex_matches.csv`. One set of rows per match (deduped on the
session id, or a name+damage fingerprint when no id is shown).

It never touches the Apex process, never reads game memory, and never sends input
to the game — it only captures the screen (like OBS) and writes a CSV. Safe to run
alongside Easy Anti-Cheat; keep it that way (don't add input automation / memory reads).

## Download (no Python needed)
Grab the latest **`ApexTracker_share.zip`** from the
[Releases page](https://github.com/yzRobo/ApexAutomatedStats/releases), unzip it,
and double-click **`ApexTrackerUI.exe`** (or `Start Tracker.bat`). No Python or
install required. (Windows SmartScreen may warn about the unsigned app the first
time: *More info -> Run anyway*.)

The app has a Start/Stop button, a live status/heartbeat, a built-in update check,
and Settings to edit the squad roster + resolution - no need to touch `config.json`.
Prefer a console? `ApexTracker.exe watch` still works.

## Updating
The app's **Check for updates** button tells you if a newer version exists and, if
so, opens the [Releases page](https://github.com/yzRobo/ApexAutomatedStats/releases).
It does **not** update itself - you swap the files once, which keeps all your data:

1. Click **Stop** (or close the app) so the files aren't in use.
2. Download the new **`ApexTracker_share.zip`** and unzip it somewhere temporary.
3. From the new folder, copy **`ApexTracker.exe`**, **`ApexTrackerUI.exe`**, and the
   **`_internal`** folder into your existing install, overwriting the old ones.
4. **Leave your own files in place** - `config.json`, `.env`, and `apex_matches.csv`
   hold your roster/settings and match history, so don't overwrite them. (Copying
   only the three items in step 3 preserves them automatically.)
5. Launch `ApexTrackerUI.exe` - Check for updates should now show "Up to date".

Prefer a clean slate? You can instead unzip the new version into a fresh folder and
copy your old `config.json` (and `apex_matches.csv` if you want the history) into it.

Tuned for 1920x1080, and it **auto-scales to any 16:9 resolution** (1440p, 4K), so
most setups just work. If your numbers look off, run `ApexTracker.exe setup` to pin
your resolution, or see [CALIBRATION.md](CALIBRATION.md) to add a profile.

**Running native 16:9?** The base regions are tuned for a 16:10-in-game capture; on a
native-16:9 setup the summary screen sits differently. If nothing logs (or values are
garbled) on native 16:9, pick **`1920x1080-native-16x9`** in the GUI's Resolution
dropdown (or set `force_resolution` to it) — a built-in profile for that layout.

To rebuild the `.exe` yourself, run [`build_release.bat`](build_release.bat) - it
runs PyInstaller against [`apextracker.spec`](apextracker.spec) and assembles the
zip. To run from source without building, see below.

## Running it
Double-click **`Start Tracker.bat`**, or from a terminal:
```
py apex_tracker.py watch
```
Leave it running while you play. Each new match's summary is logged automatically.
`Ctrl+C` (or close the window) to stop. Data lands in `apex_matches.csv`.

## How it works
- **Capture:** Windows Graphics Capture (WGC — the same modern API OBS uses). It
  **auto-finds the Apex window by exe name** (`r5apex_dx12.exe`) and captures just
  that window, so it works on any monitor and in **exclusive fullscreen**, with
  minimal load on the game. (The old dxcam/Desktop-Duplication path caused stutter
  and black frames in fullscreen — that's gone.)
- A background thread keeps the latest frame; the main loop checks it ~once per
  second. A cheap gold-color + "CHAMPION" text check decides if it's the summary
  screen, so OCR only runs on that screen, not during gameplay.
- Recognition-only OCR on fixed crops; gold-isolation for the header numbers;
  tile-and-vote for lone revive/respawn digits; names snapped to known squadmates.
- If Apex restarts or its window changes, the watcher re-acquires it automatically.

## Capture modes (performance / stutter)
Capturing an **exclusive-fullscreen** game has an unavoidable cost: Windows has to
pull the game off its fast present path to let anything outside the game see its
pixels, which can cause micro-stutter. Pick the mode that suits you in the GUI's
**Capture mode** dropdown (or `capture.mode` in `config.json`):

- **Standalone — continuous (default).** Captures the monitor continuously and reads
  the end screen reliably. Simple and dependable; the trade-off is that capturing an
  exclusive-fullscreen game can cause some micro-stutter (run Apex in **Borderless
  Windowed**, or use OBS mode below, to avoid it).
- **Standalone — on-demand (BETA, opt-in).** Set `capture.on_demand: true`. Captures
  in short bursts so gameplay isn't continuously captured (less stutter), but it only
  glances at the screen every `idle_probe_seconds`, so it **can miss a match's end
  screen and fail to log it**. Off by default for that reason; only use it if you've
  confirmed it reliably logs your matches.
- **OBS Virtual Camera — zero game overhead (recommended for zero stutter).** Reads frames from OBS instead of
  capturing the screen. If you already run OBS with a **Game Capture** of Apex, the
  tracker piggybacks on the frames OBS already has — no extra load on the game, no
  stutter, and you keep exclusive fullscreen. Still fully passive: we never touch the
  game; OBS does the capture. **Setup:** in OBS add a Game Capture of Apex → click
  **Start Virtual Camera** → choose "OBS Virtual Camera" in the tracker. Set
  `capture.video_width/height` to your OBS canvas (default 1920x1080). Requires OBS
  running. Run `ApexTracker.exe devices` to confirm the camera is detected.

The other zero-stutter option is simply running Apex in **Borderless Windowed**.

## Commands
```
py apex_tracker.py monitors             # list monitors
py apex_tracker.py devices              # list video inputs (find the OBS Virtual Camera)
py apex_tracker.py shot                 # save a capture to debug/ (prove capture works)
py apex_tracker.py shot obs             # save a capture via the OBS Virtual Camera (test OBS mode)
py apex_tracker.py calibrate img.png    # draw crop boxes over a screenshot + OCR them
py apex_tracker.py batch [folder|glob]  # OCR image(s) -> debug/sample_check.csv (verification)
py apex_tracker.py watch                # run the live auto-watcher
```

## CSV columns
`timestamp, session_id, squad_placed, total_squad_kills, player_slot, name,
kills, assists, knocks, damage, revive_given, respawn_given`

- One row per player, three rows per match.
- `session_id` is the match id from the bottom-left; if it isn't on screen, a
  `fp:...` fingerprint of names+damage is used instead (still unique per match).

## config.json — things you may want to tweak
- `capture.mode`: `monitor` (Standalone, default) or `obs` (OBS Virtual Camera). See
  **Capture modes** above.
- `capture.on_demand`: `true` (default) = burst-capture so gameplay isn't continuously
  captured (less fullscreen stutter). `capture.idle_probe_seconds` sets how often it probes.
- `capture.video_device_name` / `video_device_index` / `video_width` / `video_height`:
  OBS-mode settings (auto-detects the OBS camera by name; size should match your OBS canvas).
- `capture.exe_names`: process names to auto-find (Apex is `r5apex_dx12.exe`).
- `capture.monitor_index`: fallback monitor (1-based) used only if Apex isn't found.
- `capture.throttle_ms`: min ms between captured frames in WGC modes (higher = lighter).
- `new_file_each_run`: `false` = one running file (default). `true` = a new
  timestamped file each time you start `watch` (e.g. `apex_matches_20260604_193512.csv`).
- `known_names`: your recurring squadmates' gamertags — OCR names are snapped to
  the closest one, fixing stray glyph misreads. **Add your squad's names here.**
- `poll_seconds`: how often it checks the screen (default 1.0).
- Crop regions (`columns`, `rows`, `header`, `detect`) are measured for 1920x1080
  and auto-scale via `base_width/base_height`.

## Calibration (only if numbers look off)
The regions are tuned for 1920x1080. If you ever need to re-tune:
1. Get a summary screenshot as PNG (drop in `samples\`, or run `shot` in-game).
2. `py apex_tracker.py calibrate samples\yourshot.png`
3. Open `debug\calibrate_overlay.png`, check the boxes sit on the right values,
   nudge the matching `x/y/w/h` in `config.json`, re-run.

## Accuracy
Verified 100% across the four sample screenshots for names, kills, assists, knocks,
damage, revives, respawns, placement, and total kills. Session ids read correctly or
near-correctly (an occasional thin `:` merges; dedup is unaffected).
