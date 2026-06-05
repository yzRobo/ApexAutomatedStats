@echo off
REM Double-click to start the Apex tracker. Leave this window open while you play.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
py -u apex_tracker.py watch
echo.
echo Tracker stopped. Press any key to close.
pause >nul
