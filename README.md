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
add your in-game name to `known_names` in `config.json`, and double-click
**`Start Tracker.bat`**. No Python or install required. (Windows SmartScreen may
warn about the unsigned app the first time: *More info -> Run anyway*.) Built for
1920x1080.

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

## Commands
```
py apex_tracker.py monitors             # list monitors
py apex_tracker.py shot                 # save a capture of each monitor to debug/ (prove capture works)
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
- `capture.exe_names`: process names to auto-find (Apex is `r5apex_dx12.exe`).
- `capture.monitor_index`: fallback monitor (1-based) used only if Apex isn't found.
- `capture.throttle_ms`: min ms between captured frames (higher = lighter; 250 = ~4/s).
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
