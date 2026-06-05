# Calibrating the tracker for a new resolution

## Do I even need to calibrate?
Probably not for 16:9. The tracker measures every crop region at **1920x1080** and
**auto-scales** them to whatever resolution it captures. Since 2560x1440 and
3840x2160 are the *same 16:9 shape*, scaling lands on the right spots on its own in
most cases.

**Only calibrate if**, at the target resolution, the logged numbers come out wrong,
blank, or garbled. **Ultrawide (21:9** - e.g. 3440x1440) is *not* 16:9 and will
need a profile.

## How the tracker picks regions each run
- It detects the captured resolution and looks for `profiles["WIDTHxHEIGHT"]` in
  `config.json`.
- **Match found** -> uses that profile's regions as-is (no scaling).
- **No match** -> scales the base 1920x1080 regions to fit (the default path).
- Pin a resolution with **`setup`** (interactive) or **`--res 2560x1440`** on any
  command. Leave `force_resolution` empty in config to auto-detect every run.

```
ApexTracker.exe setup                 # asks your resolution, saves it
ApexTracker.exe watch --res 2560x1440 # force a profile for one run
```

## Step 1 - get a summary screenshot at the target resolution
On the machine running that resolution, on the post-match **CHAMPIONS / SQUAD
ELIMINATED** summary screen, do either:
- Run `ApexTracker.exe shot` (or from source `py apex_tracker.py shot`) - it saves a
  capture into `debug/`; **or**
- Take a normal screenshot (Steam/Windows) of the summary screen and save the PNG.

Requirements for a good calibration shot: full native resolution, all three player
cards visible, and the bottom-left match/session id visible. Drop it in `samples/`.

## Step 2 - check how far off scaling already is
```
py apex_tracker.py calibrate samples\theirshot.png
```
This writes `debug/calibrate_overlay.png` (the crop boxes drawn on the image) and
prints the OCR'd values. Open the overlay:
- Boxes sit correctly and printed values are right -> **done, no profile needed.**
- Boxes are shifted / values wrong -> build a profile (Step 3).

## Step 3 - build a profile (only if Step 2 was off)
A profile is a complete region set measured at that native resolution.

1. **Start from scaled values.** Multiply each `x/y/w/h` in the base `detect`,
   `header`, `columns`, `rows` blocks by `target_width / 1920`
   (2560x1440 = x1.3333, 3840x2160 = x2.0).
2. Add them under `profiles` in `config.json`, keyed by resolution, with that
   resolution as the profile's own base:
   ```json
   "profiles": {
     "2560x1440": {
       "base_width": 2560,
       "base_height": 1440,
       "detect":  { "...copy + adjust from the base detect block..." },
       "header":  { "squad_placed": {}, "total_kills": {}, "session_id": {} },
       "columns": [ {}, {}, {} ],
       "rows":    { "name": {}, "kak": {}, "damage": {}, "revive": {}, "respawn": {} }
     }
   }
   ```
3. Re-run with the profile forced and inspect the overlay:
   ```
   py apex_tracker.py calibrate samples\theirshot.png --res 2560x1440
   ```
4. Nudge the `x/y/w/h` until every box lines up and every printed value is correct.
   Repeat until 100%.

Tip: copy the four blocks (`detect`, `header`, `columns`, `rows`) from the top of
`config.json` as your starting point - those are exactly what a profile needs.

## Step 4 - ship it
- Commit the updated `config.json`.
- Re-run `build_release.bat` to bake the new config into a fresh
  `ApexTracker_share.zip`, and publish a new GitHub Release.
- Friends on that resolution either run `setup` once or just let it auto-select by
  detected resolution.
- Remember to add the friend's gamertag to `known_names` so OCR snaps their name.

---

## The easy button: have Claude build the profile
Open this repo in Claude Code, attach the screenshot(s), and paste this prompt:

> I'm adding a calibration profile for **RESOLUTION** (e.g. 2560x1440) to the Apex
> tracker. I've saved a post-game summary screenshot at that resolution to
> `samples/FILENAME`. Please:
> 1. Run `py apex_tracker.py calibrate samples/FILENAME --res RESOLUTION` and open
>    `debug/calibrate_overlay.png`.
> 2. Compare the OCR'd values against what's actually on the screenshot: each
>    player's name, kills/assists/knocks, damage, revives, respawns, plus squad
>    placement, total squad kills, and the bottom-left session id.
> 3. Add or adjust a `profiles["RESOLUTION"]` block in `config.json` (with its own
>    `base_width`/`base_height` and the `detect`/`header`/`columns`/`rows` regions)
>    until every field reads correctly.
> 4. Re-run `calibrate` to confirm 100%, then sync `config.json` into the
>    `ApexAutomatedStats` repo. Don't rebuild or cut a release unless I ask.
>
> Keep it strictly passive / read-only - screen capture and CSV only, no game
> interaction.
