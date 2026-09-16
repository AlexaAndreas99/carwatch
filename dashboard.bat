@echo off
rem The CarWatch dashboard, in its own window. Closing the window stops it.
rem
rem Started minimised by open-carwatch.ps1 (the desktop icon); double-clicking
rem this file directly works too. Port 8009, so bookmarks keep working.
title CarWatch dashboard - close this window to stop it
cd /d "%~dp0"
".venv\Scripts\python.exe" -m carwatch.web --port 8009
if errorlevel 1 pause
