@echo off
REM CarWatch collector wrapper (Windows cmd.exe).
REM
REM A thin shim over run.ps1, for anywhere a .bat is easier than a .ps1 -
REM notably a Task Scheduler action that shouldn't have to name powershell.exe
REM and an ExecutionPolicy flag.
REM
REM   run.bat                                     - all enabled searches
REM   run.bat --status                            - config + DB state
REM   run.bat --search "Nissan Qashqai 2025 4x4 Tekna"
REM
REM Exit code is the collector's own: 0 all ok, 2 bad config,
REM 5 at least one source blocked or errored.

setlocal
set "ROOT=%~dp0"

REM -ExecutionPolicy Bypass so a fresh machine doesn't refuse to run the script;
REM it applies to this process only and changes nothing system-wide.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%run.ps1" %*
exit /b %ERRORLEVEL%
