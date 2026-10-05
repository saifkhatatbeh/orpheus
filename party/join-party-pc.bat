@echo off
rem Orpheus Party — native snapclient for this PC (Edifier output)
rem ~1ms sync. Runs in the background; close from system tray / task manager.
cd /d "%~dp0snapclient"
start "Orpheus Party Client" /min snapclient.exe -h 127.0.0.1 --hostID "EDIFIER-PC"
echo Party client launched (EDIFIER-PC). It will appear in NODES ^> PARTY ENGINE.
