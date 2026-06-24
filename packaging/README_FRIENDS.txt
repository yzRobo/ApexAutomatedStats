============================================================
  Apex Damage / Kill Tracker  -  Quick Start
============================================================

What it does: while you play Apex, it watches your screen, reads the
post-game summary screen (kills, damage, knocks, etc.), and logs it.
It is passive and read-only - it never touches the game, never reads
game memory, and never sends any input. Same kind of screen capture
OBS uses. Safe to run with Easy Anti-Cheat.

You do NOT need Python or anything else installed.

------------------------------------------------------------
SETUP
------------------------------------------------------------
1. Unzip this whole folder somewhere (e.g. your Desktop).
   Keep all the files together - don't move the .exe out on its own.

2. Double-click  ApexTrackerUI.exe  (or Start Tracker.bat).
   The app window opens.

3. In the app's Settings, add your in-game name to the roster
   (type it in and click "Add name"), pick your Resolution if you
   want, and click "Save settings".

4. (Optional - for Rank/RP tracking) In Settings, paste your own
   FREE ALS API key into the "ALS API key" box and Save. Get one at
   apexlegendsstatus.com -> sign in -> Settings -> API. Without a key,
   kills/damage/placement still log fine; only Rank/RP needs it.

------------------------------------------------------------
USING IT
------------------------------------------------------------
- Click  Start  in the app, then go play. The status dot shows it's
  watching (green = capture OK). After each match's summary screen,
  the stats are logged automatically and show up in our shared squad
  stats. Click  Stop  (or just close the window) when you're done.
- "Open CSV" shows your matches saved locally (apex_matches.csv).
- "Check for updates" tells you if a newer version is available.

------------------------------------------------------------
UPDATING
------------------------------------------------------------
"Check for updates" will tell you when there's a new version, but it
does NOT update itself. To update (this keeps your settings + history):
  1. Close the app.
  2. Download the new ApexTracker_share.zip and unzip it somewhere.
  3. Copy ApexTracker.exe, ApexTrackerUI.exe, and the _internal folder
     from the new unzip into your existing folder, overwriting them.
  4. Do NOT overwrite config.json, .env, or apex_matches.csv - those are
     your roster/settings and match history. (Copying only the 3 items
     in step 3 leaves them untouched.)
  5. Reopen ApexTrackerUI.exe.

------------------------------------------------------------
NOTES
------------------------------------------------------------
* First launch: Windows may show a blue "Windows protected your PC"
  box because the app isn't signed. Click "More info" -> "Run anyway".
  (It's just an unsigned app from a friend, not a virus.)

* Built for 1920x1080 and auto-scales to any 16:9 resolution (1440p,
  4K). If your numbers ever look off, set your Resolution in Settings
  or ping yzRobo.

* It only does work when the end-of-match summary is on screen, so it
  has basically no impact on your game while you're playing.

* Prefer a plain console instead of the app? Run:  ApexTracker.exe watch
============================================================
