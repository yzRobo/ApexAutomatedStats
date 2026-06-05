@echo off
REM ===========================================================================
REM Builds the shareable, no-Python-required distributable for friends.
REM Output: release\ApexTracker\  (folder)  and  release\ApexTracker_share.zip
REM Run from this folder:  build_release.bat
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo [1/5] Installing/standardizing build dependencies...
py -m pip install -r requirements.txt || goto :err

echo [2/5] Cleaning previous build...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist release rmdir /s /q release

echo [3/5] Running PyInstaller (this takes a few minutes)...
py -m PyInstaller --noconfirm --clean apextracker.spec || goto :err

echo [4/5] Assembling the share folder...
mkdir release
move dist\ApexTracker release\ApexTracker >nul
REM Editable, user-facing files sit NEXT TO the exe (script reads them from there):
copy /y config.json "release\ApexTracker\config.json" >nul
copy /y packaging\friend.env "release\ApexTracker\.env" >nul
copy /y "packaging\Start Tracker.bat" "release\ApexTracker\Start Tracker.bat" >nul
copy /y packaging\README_FRIENDS.txt "release\ApexTracker\README.txt" >nul

echo [5/5] Zipping...
powershell -NoProfile -Command "Compress-Archive -Path 'release\ApexTracker' -DestinationPath 'release\ApexTracker_share.zip' -Force" || goto :err

echo.
echo DONE. Hand this file to friends:  release\ApexTracker_share.zip
echo (They unzip, add their gamertag to config.json, run Start Tracker.bat.)
goto :eof

:err
echo.
echo BUILD FAILED. See the error above.
exit /b 1
