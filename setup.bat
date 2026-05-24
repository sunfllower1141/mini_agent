@echo off
REM setup.bat — full bootstrap for mini_agent (Windows)
REM Run: setup.bat
REM
REM This script:
REM   1. Checks for required system tools (Node.js, Python, ripgrep)
REM   2. Creates a Python virtual environment and installs dependencies
REM   3. Installs Node.js packages and builds the Electron renderer
REM   4. Gets you ready to launch with a single command
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

echo [0/5] Checking prerequisites...

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

echo [1/5] Python virtual environment...
if not exist "venv\" (
    python -m venv venv
    echo   [OK] Created venv\
) else (
    echo   [OK] venv\ already exists, skipping
)

REM ------------------------------------------------------------------    
REM 2. Python dependencies
REM ------------------------------------------------------------------    

echo [2/5] Python dependencies...
call venv\Scripts\activate.bat
pip install --upgrade pip -q
pip install -r requirements.txt -q
echo   [OK] Installed Python packages

REM ------------------------------------------------------------------    
REM 3. Node.js dependencies
REM ------------------------------------------------------------------    

echo [3/5] Node.js dependencies...
cd mini_agent_electron
call npm install --silent
echo   [OK] Installed npm packages

REM ------------------------------------------------------------------    
REM 4. Build Electron renderer
REM ------------------------------------------------------------------    

echo [4/5] Building renderer...
call npm run build --silent
echo   [OK] Renderer built → mini_agent_electron\renderer\dist\
cd ..

REM ------------------------------------------------------------------    
REM 5. API key check
REM ------------------------------------------------------------------    

echo [5/5] API key check...

set KEY_FOUND=0
if defined DEEPSEEK_API_KEY ( echo   [OK] DEEPSEEK_API_KEY is set & set KEY_FOUND=1 )
if defined CLAUDE_API_KEY    ( echo   [OK] CLAUDE_API_KEY is set    & set KEY_FOUND=1 )
if defined XAI_API_KEY       ( echo   [OK] XAI_API_KEY is set       & set KEY_FOUND=1 )
if defined OLLAMA_API_KEY    ( echo   [OK] OLLAMA_API_KEY is set    & set KEY_FOUND=1 )

if !KEY_FOUND! equ 0 (
    if exist "%USERPROFILE%\.mini_agent_env" (
        findstr /R "DEEPSEEK_API_KEY CLAUDE_API_KEY XAI_API_KEY OLLAMA_API_KEY" "%USERPROFILE%\.mini_agent_env" >nul 2>nul
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
pause
