@echo off
title ORPHEUS SOUND SYSTEM
cd /d "%~dp0"

rem ---- first run: create venv + install deps ----
if not exist ".venv\Scripts\python.exe" (
  echo   [setup] creating virtualenv...
  python -m venv .venv || (echo   python 3.11+ required & pause & exit /b 1)
  .venv\Scripts\python.exe -m pip install -q --upgrade pip
  .venv\Scripts\python.exe -m pip install -q -r requirements.txt
)

rem ---- party engine assets (snapclient + snapweb), skipped if present ----
if not exist "party\snapclient\snapclient.exe" (
  .venv\Scripts\python.exe party\setup_party.py
)

rem ---- show the LAN address so phones can join ----
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4" ^| findstr /v "127.0.0.1"') do set LANIP=%%a
set LANIP=%LANIP: =%

echo.
echo   ORPHEUS starting on http://localhost:7777
if defined LANIP echo   Phone / iPad:  http://%LANIP%:7777
echo.
.venv\Scripts\python.exe -m server.main
