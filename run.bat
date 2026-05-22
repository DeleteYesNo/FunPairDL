@echo off
REM FunPairDL launcher — verifies dependencies on first run, then starts the app.
REM Runs from this file's own folder, so it works wherever the repo is cloned.
cd /d "%~dp0"

REM 1. Make sure Python is available on PATH
where python >nul 2>nul
if errorlevel 1 (
    echo [FunPairDL] Python was not found on PATH.
    echo Install Python 3.11+ from https://www.python.org/downloads/ and tick
    echo "Add python.exe to PATH" during setup, then run this file again.
    pause
    exit /b 1
)

REM 2. Check required packages; install them only if something is missing
python -c "import importlib.util,sys; m=['PySide6','aiohttp','yt_dlp','fastapi','uvicorn','pydantic','qasync','mega']; sys.exit(1 if any(importlib.util.find_spec(x) is None for x in m) else 0)"
if errorlevel 1 (
    echo [FunPairDL] Installing dependencies, this only happens once...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [FunPairDL] Dependency installation failed. See the messages above.
        pause
        exit /b 1
    )
)

REM 3. First run: create config.json from the template if it doesn't exist
if not exist "config.json" (
    copy /y "config.example.json" "config.json" >nul
    echo [FunPairDL] Created config.json from the template. Edit it to add the
    echo            credentials for the services you use ^(optional^).
)

REM 4. Launch without a lingering console window
start "" pythonw "FunPairDL.pyw"
