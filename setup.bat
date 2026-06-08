@echo off
REM setup.bat — full bootstrap for mini_agent (Windows)
REM Run: setup.bat
REM
REM This script:
REM   1. Checks for required system tools (Node.js, Python, ripgrep, git)
REM   2. Creates a Python virtual environment and installs dependencies
REM   3. Installs Playwright browser binaries
REM   4. Installs Node.js packages
REM   5. Builds the Electron renderer
REM   6. Checks for .env / API keys
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ===================================
echo      mini_agent — setup (Windows)
echo ===================================
echo.

REM ------------------------------------------------------------------
REM 0. Prerequisite checks
REM ------------------------------------------------------------------

echo [0/7] Checking prerequisites...

set ERRORS=0

REM Python — find the real Python, not the Microsoft Store stub
set PYTHON_EXE=
set PYTHON_VER=

REM Check common real Python install locations first
for %%p in (
    "C:\Users\%USERNAME%\AppData\Local\Python\bin\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python313\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python312\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python311\python.exe"
    "C:\Users\%USERNAME%\AppData\Local\Programs\Python\Python310\python.exe"
    "C:\Program Files\Python313\python.exe"
    "C:\Program Files\Python312\python.exe"
    "C:\Program Files\Python311\python.exe"
    "C:\Program Files\Python310\python.exe"
) do (
    if exist %%p (
        set "PYTHON_EXE=%%~p"
        goto :python_found
    )
)

REM Last resort: use 'where python' but skip the WindowsApps stub
for /f "delims=" %%p in ('where python 2^>nul') do (
    echo %%p | findstr /I "WindowsApps" >nul
    if !errorlevel! neq 0 (
        set "PYTHON_EXE=%%p"
        goto :python_found
    )
)

REM No real Python found
echo   [MISSING] Python 3 not found. Install from https://www.python.org/downloads/
echo            Make sure to check "Add Python to PATH" during install.
echo            If Python IS installed, disable App Execution Aliases:
echo              Settings ^> Apps ^> Advanced app settings ^> App execution aliases
echo              Turn OFF "python.exe" and "python3.exe"
set /a ERRORS+=1
goto :python_done

:python_found
for /f "tokens=2" %%i in ('"%PYTHON_EXE%" --version 2^>^&1') do set PYTHON_VER=%%i
echo   [OK] Python  (!PYTHON_VER!)  [%PYTHON_EXE%]

:python_done

REM Node.js — check version (Electron 42 requires Node ≥ 22)
for /f "tokens=1 delims=v" %%v in ('node --version 2^>nul') do set NODE_VER=%%v
if defined NODE_VER (
    for /f "tokens=1 delims=." %%m in ("!NODE_VER!") do set NODE_MAJOR=%%m
    if !NODE_MAJOR! geq 22 (
        echo   [OK] Node.js !NODE_VER!
    ) else (
        echo   [FAIL] Node.js !NODE_VER! is too old.
        echo          Electron 42 requires Node.js ^>= 22.
        echo          Install from https://nodejs.org (latest LTS)
        echo          If already installed, you may need to upgrade.
        set /a ERRORS+=1
    )
) else (
    echo   [MISSING] Node.js not found.
    echo     Install from https://nodejs.org (v22+ LTS)
    echo     If already installed, check your PATH or re-run the installer.
    set /a ERRORS+=1
)

REM npm — check version (vite 8 needs npm ≥ 9)
for /f "tokens=1" %%v in ('npm --version 2^>nul') do set NPM_VER=%%v
if defined NPM_VER (
    for /f "tokens=1 delims=." %%m in ("!NPM_VER!") do set NPM_MAJOR=%%m
    if !NPM_MAJOR! geq 9 (
        echo   [OK] npm v!NPM_VER!
    ) else (
        echo   [FAIL] npm v!NPM_VER! is too old. Need npm ^>= 9.
        echo          Update with: npm install -g npm@latest
        set /a ERRORS+=1
    )
) else (
    echo   [MISSING] npm not found (bundled with Node.js)
    set /a ERRORS+=1
)

REM ripgrep (strongly recommended)
where rg >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] ripgrep
) else (
    echo   [WARN] ripgrep (rg) not found.
    echo          Install: winget install BurntSushi.ripgrep.MSVC
    echo          Without it, file search falls back to slower methods.
)

REM Git
where git >nul 2>nul
if %errorlevel% equ 0 (
    echo   [OK] git
) else (
    echo   [WARN] git not found. Some agent tools (git skill, branch detection) won't work.
    echo         Install: winget install Git.Git
)

if !ERRORS! gtr 0 (
    echo.
    echo Missing !ERRORS! required tool(s). Please install them and re-run setup.
    echo Press any key to exit...
    pause >nul
    exit /b 1
)
:skip_pip

echo.

REM ------------------------------------------------------------------
REM 1. Python virtual environment
REM ------------------------------------------------------------------

echo [1/7] Python virtual environment...
if not defined PYTHON_EXE (
    echo   [SKIP] No Python found, cannot create venv
    goto :skip_venv
)
if not exist "venv\" (
    "%PYTHON_EXE%" -m venv venv
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
:skip_venv

REM ------------------------------------------------------------------
REM 2. Python dependencies
REM ------------------------------------------------------------------

echo [2/7] Python dependencies...
if not exist "venv\Scripts\python.exe" (
    echo   [SKIP] No venv found, cannot install Python packages
    goto :skip_pip
)
call venv\Scripts\activate.bat
venv\Scripts\python.exe -m pip install --upgrade pip -q
venv\Scripts\pip.exe install -r requirements.txt -q
if %errorlevel% equ 0 (
    echo   [OK] Installed Python packages
) else (
    echo   [FAIL] pip install failed.
    echo.
    echo   Some packages (sentence-transformers) may need Visual C++ Build Tools:
    echo     https://visualstudio.microsoft.com/visual-cpp-build-tools/
    echo   Select "Desktop development with C++" during install.
    echo.
    pause
    exit /b 1
)

REM ------------------------------------------------------------------
REM 3. Playwright browser
REM ------------------------------------------------------------------

echo [3/7] Playwright browser...
if not exist "venv\Scripts\python.exe" (
    echo   [SKIP] No venv found, cannot install Playwright browsers
    goto :skip_playwright
)
venv\Scripts\python.exe -m playwright install chromium --with-deps 2>nul
if %errorlevel% equ 0 (
    echo   [OK] Chromium browser installed for Playwright
) else (
    echo   [WARN] Playwright browser install failed. Web browsing tools won't work.
    echo          You can retry later: venv\Scripts\python.exe -m playwright install chromium
)
:skip_playwright

REM ------------------------------------------------------------------
REM 4. Node.js dependencies
REM ------------------------------------------------------------------

echo [4/7] Node.js dependencies...
if not exist "mini_agent_electron\" (
    echo   [FAIL] mini_agent_electron\ directory not found. Are you in the repo root?
    pause
    exit /b 1
)

cd mini_agent_electron

REM Clean up a broken node_modules from a previous failed install.
REM Electron's binary download (~100 MB) can fail halfway — if the
REM electron dist/ directory is missing, the install is incomplete.
if exist "node_modules\" (
    if not exist "node_modules\electron\dist\electron.exe" (
        echo   [WARN] node_modules\ exists but Electron binary is missing.
        echo          Removing broken node_modules\ to reinstall cleanly...
        rmdir /s /q "node_modules" 2>nul
    )
)

if exist "node_modules\" (
    echo   [OK] node_modules\ already exists, updating...
    call npm install --prefer-offline --no-audit --no-fund
) else (
    echo   Installing Electron + renderer packages...
    echo   This will download Electron (~100 MB). It may take a few minutes.
    call npm install --prefer-offline --no-audit --no-fund
)

if %errorlevel% neq 0 (
    echo.
    echo   [FAIL] npm install failed.
    echo.
    echo   Common Windows issues:
    echo   - PATH too long (Windows MAX_PATH=260). Move the repo closer to
    echo     the drive root, e.g. C:\mini_agent\ instead of a deep folder.
    echo   - Corporate proxy blocking npm registry or GitHub releases.
    echo     Configure proxy: npm config set proxy http://proxy:port
    echo     Configure Electron mirror: set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
    echo   - Antivirus blocking the Electron binary download.
    echo     Temporarily disable real-time scanning and retry.
    echo   - Old npm cache: npm cache clean --force  then retry.
    echo.
    cd ..
    pause
    exit /b 1
)

REM Verify Electron installed correctly (binary exists and is executable)
if exist "node_modules\electron\dist\electron.exe" (
    echo   [OK] Electron binary found
) else (
    echo.
    echo   [FAIL] Electron binary not found after npm install.
    echo          node_modules\electron\dist\electron.exe is missing.
    echo.
    echo   This usually means the Electron download was blocked or corrupted.
    echo   Try: rmdir /s /q node_modules
    echo        set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
    echo        npm install
    echo.
    cd ..
    pause
    exit /b 1
)

REM Quick smoke test: ask electron for its version
node_modules\.bin\electron --version >nul 2>&1
if !errorlevel! equ 0 (
    for /f %%v in ('node_modules\.bin\electron --version 2^>^&1') do (
        echo   [OK] Electron %%v works
    )
) else (
    echo   [WARN] Electron binary exists but failed to run. This may be a
    echo          missing Visual C++ redistributable. Install from:
    echo          https://aka.ms/vs/17/release/vc_redist.x64.exe
)

REM ------------------------------------------------------------------
REM 5. Build Electron renderer
REM ------------------------------------------------------------------

echo [5/7] Building renderer...
call npm run build
if %errorlevel% equ 0 (
    echo   [OK] Renderer built -^> mini_agent_electron\renderer\dist\
) else (
    echo   [WARN] Renderer build failed (npm run build).
    echo          This may be a vite issue. npm start will auto-build on
    echo          first launch anyway. To debug:
    echo            cd mini_agent_electron ^&^& npx vite build
    echo.
    echo          Common fixes:
    echo          - Delete node_modules and re-run setup
    echo          - npm cache clean --force then npm install
)
cd ..

REM ------------------------------------------------------------------
REM 6. .env file check
REM ------------------------------------------------------------------

echo [6/7] Project .env file...

if exist ".env" (
    findstr /R "API_KEY=" .env >nul 2>nul
    if !errorlevel! equ 0 (
        echo   [OK] .env file found with API keys
    ) else (
        echo   [OK] .env file exists but no API_KEY entries detected
    )
) else (
    echo   [INFO] No .env file in project root (optional).
    echo          Create one to persist your API keys:
    echo            notepad .env
    echo.
    echo          Example content:
    echo            DEEPSEEK_API_KEY=sk-...
    echo            CLAUDE_API_KEY=sk-ant-...
)

REM ------------------------------------------------------------------
REM 7. API key check
REM ------------------------------------------------------------------

echo [7/7] API key check...

set KEY_FOUND=0
if defined DEEPSEEK_API_KEY ( echo   [OK] DEEPSEEK_API_KEY is set & set KEY_FOUND=1 )
if defined CLAUDE_API_KEY    ( echo   [OK] CLAUDE_API_KEY is set    & set KEY_FOUND=1 )
if defined XAI_API_KEY       ( echo   [OK] XAI_API_KEY is set       & set KEY_FOUND=1 )
if defined OLLAMA_API_KEY    ( echo   [OK] OLLAMA_API_KEY is set    & set KEY_FOUND=1 )
if defined OPENAI_API_KEY    ( echo   [OK] OPENAI_API_KEY is set    & set KEY_FOUND=1 )

REM Also check %USERPROFILE%\.mini_agent_env (written by the app's settings panel)
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
    echo   On first launch the app shows a settings panel where you can enter
    echo   your key. Supported providers: DeepSeek, Claude, xAI, Ollama.
    echo.
    echo   Or set one now in this terminal before launching:
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
echo ---------------------------------------------------
echo   Windows tips
echo ---------------------------------------------------
echo - First launch may trigger a Windows Defender Firewall popup
echo   (Node.js needs network for the agent API). Click "Allow".
echo.
echo - If the window is blank/white, check that the renderer built:
echo     cd mini_agent_electron ^&^& npm run build
echo.
echo - Keyboard shortcuts:
echo     Enter        Submit message
echo     Shift+Enter  New line
echo     Escape       Cancel streaming response
echo.
pause
