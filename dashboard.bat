@echo off
rem The CarWatch dashboard, in its own window. Closing the window stops it.
rem
rem The desktop icon no longer uses this - it starts the dashboard with no
rem window (carwatch\launch.py). Double-clicking this file still works, for when
rem you want to watch the output. Port 8009, so bookmarks keep working.
title CarWatch dashboard - close this window to stop it
cd /d "%~dp0"
".venv\Scripts\python.exe" -m carwatch.web --port 8009
if errorlevel 1 pause
