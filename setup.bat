@echo off
REM setup.bat — full bootstrap for mini_agent (Windows)
REM Run: setup.bat
REM
REM This script:
REM   1. Checks for required system tools (Node.js, Python, ripgrep)
REM   2. Creates a Python virtual environment and installs dependencies
REM   3. Installs Node.js packages
REM   4. Builds the Electron renderer (optional — npm start also does this)
REM   5. Guides you through API key setup
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ===================================
echo      mini_agent — setup
echo ===================================
echo.

REM ------------------------------------------------------------------
REM 0. Prerequisite checks
REM ------------------------------------------------------------------

echo [0/6] Checking prerequisites...

set ERRORS=0

REM Python
where python >nul 2>nul
if %errorlevel% equ 0 (
    for /f "tokens=2" %%i in ('python --version 2^>^&1') do set PY_VER=%%i
    echo   [OK] Python  (!PY_VER!)
) else (
    echo   [MISSING] Python 3 not found. Install from https://www.python.org/downloads/
    set /a ERRORS+=1
)

REM Node.js
where node >nul 2>nul
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('node --version 2^>^&1') do set NODE_VER=%%i
    echo   [OK] Node.js  (!NODE_VER!)
) else (
    echo   [MISSING] Node.js not found. Install from https://nodejs.org (v18+)
    set /a ERRORS+=1
)

REM npm
where npm >nul 2>nul
if %errorlevel% equ 0 (
    for /f "delims=" %%i in ('npm --version 2^>^&1') do set NPM_VER=%%i
    echo   [OK] npm      (v!NPM_VER!)
) else (
    echo   [MISSING] npm not found (usually bundled with Node.js)
    set /a ERRORS+=1
)

REM ripgrep
where rg >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] ripgrep
) else (
    echo   [WARN] ripgrep (rg) not found. Install: winget install BurntSushi.ripgrep.MSVC
    echo          Without it, file search will fall back to slower methods.
)

REM Git (needed by some tools)
where git >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] git
) else (
    echo   [WARN] git not found. Some agent tools (git skill) won't work.
)

if !ERRORS! gtr 0 (
    echo.
    echo Missing !ERRORS! required tool(s). Please install them and re-run setup.
    pause
    exit /b 1
)

echo.

REM ------------------------------------------------------------------
REM 1. Python virtual environment
REM ------------------------------------------------------------------

echo [1/6] Python virtual environment...
if not exist "venv\" (
    python -m venv venv
    if %errorlevel% equ 0 (
        echo   [OK] Created venv\
    ) else (
        echo   [FAIL] Could not create venv\. Check your Python installation.
        pause
        exit /b 1
    )
) else (
    echo   [OK] venv\ already exists, skipping
)

REM ------------------------------------------------------------------
REM 2. Python dependencies
REM ------------------------------------------------------------------

echo [2/6] Python dependencies...
call venv\Scripts\activate.bat
python -m pip install --upgrade pip -q
pip install -r requirements.txt -q
if %errorlevel% equ 0 (
    echo   [OK] Installed Python packages
) else (
    echo   [FAIL] pip install failed. Check requirements.txt and try again.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM 3. Node.js dependencies
REM ------------------------------------------------------------------

echo [3/6] Node.js dependencies...
if not exist "mini_agent_electron\" (
    echo   [FAIL] mini_agent_electron\ directory not found. Are you in the repo root?
    pause
    exit /b 1
)

cd mini_agent_electron

REM Skip npm install if node_modules already exists (faster re-runs)
if exist "node_modules\" (
    echo   [OK] node_modules\ already exists, checking for updates...
    call npm install --silent
) else (
    call npm install --silent
)

if %errorlevel% equ 0 (
    echo   [OK] Installed npm packages
) else (
    echo   [FAIL] npm install failed. Check your Node.js version (need v18+).
    cd ..
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM 4. Build Electron renderer
REM ------------------------------------------------------------------

echo [4/6] Building renderer...
call npm run build --silent
if %errorlevel% equ 0 (
    echo   [OK] Renderer built -^> mini_agent_electron\renderer\dist\
) else (
    echo   [WARN] Renderer build failed. npm start will auto-build on first launch.
)
cd ..

REM ------------------------------------------------------------------
REM 5. .env file check (project root)
REM ------------------------------------------------------------------

echo [5/6] Project .env file...

if exist ".env" (
    findstr /R "API_KEY=" .env >nul 2>nul
    if !errorlevel! equ 0 (
        echo   [OK] .env file found with API keys
    ) else (
        echo   [OK] .env file exists but no API_KEY entries detected
    )
) else (
    echo   [INFO] No .env file in project root (optional)
    echo          Create one to persist API keys:  notepad .env
)

REM ------------------------------------------------------------------
REM 6. API key check
REM ------------------------------------------------------------------

echo [6/6] API key check...

set KEY_FOUND=0
if defined DEEPSEEK_API_KEY ( echo   [OK] DEEPSEEK_API_KEY is set & set KEY_FOUND=1 )
if defined CLAUDE_API_KEY    ( echo   [OK] CLAUDE_API_KEY is set    & set KEY_FOUND=1 )
if defined XAI_API_KEY       ( echo   [OK] XAI_API_KEY is set       & set KEY_FOUND=1 )
if defined OLLAMA_API_KEY    ( echo   [OK] OLLAMA_API_KEY is set    & set KEY_FOUND=1 )
if defined OPENAI_API_KEY    ( echo   [OK] OPENAI_API_KEY is set    & set KEY_FOUND=1 )

if !KEY_FOUND! equ 0 (
    if exist "%USERPROFILE%\.mini_agent_env" (
        findstr /R "DEEPSEEK_API_KEY CLAUDE_API_KEY XAI_API_KEY OLLAMA_API_KEY OPENAI_API_KEY" "%USERPROFILE%\.mini_agent_env" >nul 2>nul
        if !errorlevel! equ 0 (
            echo   [OK] API key found in %%USERPROFILE%%\.mini_agent_env
            set KEY_FOUND=1
        )
    )
)

if !KEY_FOUND! equ 0 (
    echo.
    echo   [WARN] No API key detected.
    echo.
    echo   The app will show a settings panel on first launch where you can
    echo   enter your key. Supported providers: DeepSeek, Claude, xAI, Ollama.
    echo.
    echo   Alternatively, set one now:
    echo     set DEEPSEEK_API_KEY=sk-...
    echo.
)

echo.

REM ------------------------------------------------------------------
REM Done
REM ------------------------------------------------------------------

echo ===================================
echo      Setup complete!
echo ===================================
echo.
echo To launch the desktop app:
echo.
echo   cd mini_agent_electron ^&^& npm start
echo.
echo For development mode (hot-reload renderer + DevTools):
echo.
echo   cd mini_agent_electron ^&^& npm run dev
echo.
echo Keyboard shortcuts in the app:
echo   Enter        Submit message
echo   Shift+Enter  New line
echo   Escape       Cancel streaming response
echo.
pause
