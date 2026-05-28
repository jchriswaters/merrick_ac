@echo off
REM HVAC HMI launcher for Windows.
REM First run: creates a venv, installs deps, writes config.json from example.
REM Subsequent runs: just activates the venv and starts the server.

cd /d "%~dp0"

if not exist .venv (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
    call .venv\Scripts\activate.bat || goto :error
    echo Installing dependencies...
    python -m pip install --upgrade pip || goto :error
    pip install -r requirements.txt || goto :error
) else (
    call .venv\Scripts\activate.bat || goto :error
)

if not exist config.json (
    echo Creating config.json from config.json.example
    echo ^>^>^> Edit config.json with your controller IP and password, then re-run ^<^<^<
    copy config.json.example config.json >nul
    pause
    exit /b 0
)

echo.
echo Starting HMI server. Open http://localhost:8000 in your browser.
echo Press Ctrl+C to stop.
echo.
python server.py
goto :eof

:error
echo.
echo *** Setup failed. See errors above. ***
pause
exit /b 1
