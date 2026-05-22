@echo off
REM FunPairDL launcher — double-click to start the app (no console window).
REM Runs from this file's own folder, so it works wherever the repo is cloned.
cd /d "%~dp0"
start "" pythonw "FunPairDL.pyw"
