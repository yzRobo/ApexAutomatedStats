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
SETUP (one time, 30 seconds)
------------------------------------------------------------
1. Unzip this whole folder somewhere (e.g. your Desktop).
   Keep all the files together - don't move the .exe out on its own.

2. Open  config.json  in Notepad and find "known_names".
   Add your in-game name to the list, e.g.:

        "known_names": [
          "yzRobo",
          "UltVaultHunter",
          "YourGamerTagHere"
        ],

   (This just helps it spell your name right. Save and close.)

------------------------------------------------------------
RUNNING IT
------------------------------------------------------------
Double-click  Start Tracker.bat  (or  ApexTracker.exe).

A black window opens and says it's watching. Leave it open and go play.
After each match's summary screen, it logs the stats automatically and
they show up in our shared squad stats. Close the window to stop.

Your matches are also saved locally to  apex_matches.csv  next to the exe.

------------------------------------------------------------
NOTES
------------------------------------------------------------
* First launch: Windows may show a blue "Windows protected your PC"
  box because the app isn't signed. Click "More info" -> "Run anyway".
  (It's just an unsigned app from a friend, not a virus.)

* It's tuned for 1920x1080. If you play at that resolution you're set.
  Other resolutions may need a quick calibration - ping yzRobo.

* It only does work when the end-of-match summary is on screen, so it
  has basically no impact on your game while you're playing.
============================================================
