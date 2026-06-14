@echo off
cd /d "%~dp0"

echo =======================================================
echo   mini_agent -- Windows Setup
echo =======================================================
echo.

echo [1] Checking for Python...
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [FAIL] Python is not installed or not in your PATH.
    pause
    exit /b 1
)
echo [OK] Python found.

echo.
echo [2] Checking Virtual Environment Status...

REM Safety Brake 1: Check if a venv is already active in the terminal
if defined VIRTUAL_ENV (
    echo [WARN] A virtual environment is currently active in this terminal!
    echo [WARN] Running setup while a venv is active can trigger Windows Defender.
    echo [WARN] Please run 'deactivate' in your terminal, then run setup.bat again.
    pause
    exit /b 1
)

REM Safety Brake 2: Check if a venv folder already exists
if exist "venv\Scripts\python.exe" (
    echo [SKIP] Healthy venv detected. Skipping creation to prevent conflicts.
) else if exist "venv\" (
    echo [WARN] A broken or incomplete 'venv' folder exists!
    echo [WARN] If this folder was copied/moved, it is corrupted.
    echo [WARN] Delete the 'venv' folder completely, then run setup.bat again.
    pause
    exit /b 1
) else (
    echo Creating new virtual environment...
    python -m venv venv
    if %errorlevel% neq 0 (
        echo [FAIL] Could not create venv.
        pause
        exit /b 1
    )
    echo [OK] Virtual environment created.
)

echo.
echo [3] Installing Python Dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo [FAIL] Failed to install Python dependencies.
    pause
    exit /b 1
)

echo.
echo [4] Installing Node Dependencies...
cd mini_agent_electron
call npm install 
call npm run build
if %errorlevel% neq 0 (
    echo [FAIL] Failed to install Node dependencies.
    cd ..
    pause
    exit /b 1
)
cd ..

echo.
echo =======================================================
echo   Setup Complete! You can now start the agent.
echo   cd mini_agent_electron
echo   npm start
echo =======================================================
exit /b 0