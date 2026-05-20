@echo off
REM setup.bat — portable setup for mini_agent (Windows)
REM Run: setup.bat
cd /d "%~dp0"

echo === mini_agent setup ===

REM 1. Create venv if missing
if not exist "venv\" (
    echo [1/3] Creating virtual environment...
    python -m venv venv
) else (
    echo [1/3] venv already exists, skipping
)

REM 2. Activate and install
echo [2/3] Installing dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip -q
pip install -r requirements.txt -q

REM 3. Done
echo [3/3] Done!
echo.
echo To start:
echo   venv\Scripts\activate.bat
echo   python mini_agent.py            # terminal REPL
echo   python tui_pt.py                # TUI (prompt_toolkit)
echo.
echo Optional flags:
echo   --unrestricted   allow read/write outside workspace
echo   --stream         stream responses token-by-token
echo   --approve        ask before write/destructive ops
pause
