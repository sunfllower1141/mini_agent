@echo off
REM ============================================================================
REM setup.bat — full bootstrap for mini_agent (Windows)
REM ============================================================================
REM Run:  setup.bat
REM
REM This script installs and configures everything needed to run mini_agent:
REM
REM   STEP 0 — Check prerequisites (Python, Node.js, npm, ripgrep, git)
REM   STEP 1 — Create Python virtual environment
REM   STEP 2 — Install Python packages from requirements.txt
REM   STEP 3 — Install Chromium browser for Playwright
REM   STEP 4 — Install Node.js packages (Electron, Vite, etc.)
REM   STEP 5 — Build the Electron renderer (UI)
REM   STEP 6 — Check for .env file (API keys)
REM   STEP 7 — Check environment for API keys
REM ============================================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo ================================================================================
echo.
echo    mini_agent — Windows Setup
echo.
echo    Repository  : %~dp0
echo    Date        : %date% %time%
echo.
echo ================================================================================
echo.

REM ============================================================================
REM  STEP 0 — Prerequisite checks
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 0/7 : Checking prerequisites
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   Checking for required tools on your system...
echo.

set ERRORS=0

REM ============================================
REM  Python detection
REM ============================================
echo   [1] Python 3 — required to run the agent backend
echo       Looking for Python...

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
echo       >>> MISSING : Python 3 not found
echo.
echo       WHAT YOU NEED TO DO:
echo       ─────────────────────────────────────────────
echo       1. Download Python from:
echo          https://www.python.org/downloads/
echo.
echo       2. Run the installer. On the first screen, CHECK:
echo          [√] Add Python to PATH
echo.
echo       3. Click "Install Now" and wait for it to finish.
echo.
echo       4. If Python IS already installed, Windows may be
echo          redirecting "python" to the Microsoft Store stub.
echo          To fix this:
echo.
echo          a) Open Windows Settings
echo          b) Go to: Apps ^> Advanced app settings
echo                     ^> App execution aliases
echo          c) Turn OFF "python.exe" and "python3.exe"
echo.
echo       5. Close this window, then run setup.bat again.
echo.
set /a ERRORS=ERRORS+1
goto :python_done

:python_found
for /f "tokens=2" %%i in ('"%PYTHON_EXE%" --version 2^>^&1') do set PYTHON_VER=%%i
echo       [OK]  Python !PYTHON_VER!  —^>  %PYTHON_EXE%

:python_done
echo.

REM ============================================
REM  Node.js detection
REM ============================================
echo   [2] Node.js — required to run Electron desktop app
echo       Minimum version: v22 (Electron 42 requirement)
echo       Checking Node.js...

for /f "tokens=1 delims=v" %%v in ('node --version 2^>nul') do set NODE_VER=%%v
if not defined NODE_VER goto :node_missing
for /f "tokens=1 delims=." %%m in ("!NODE_VER!") do set NODE_MAJOR=%%m
if !NODE_MAJOR! lss 22 goto :node_old
echo       [OK]  Node.js !NODE_VER!
goto :node_done

:node_old
echo       >>> FAIL : Node.js !NODE_VER! is too old — need v22 or later
echo.
echo       WHAT YOU NEED TO DO:
echo       ─────────────────────────────────────────────
echo       Current version : !NODE_VER!
echo       Required        : v22.0.0 or later
echo.
echo       Download and install the latest LTS from:
echo         https://nodejs.org
echo.
echo       After installing, restart this terminal and
echo       run setup.bat again.
echo.
set /a ERRORS=ERRORS+1
goto :node_done

:node_missing
echo       >>> MISSING : Node.js not found
echo.
echo       WHAT YOU NEED TO DO:
echo       ─────────────────────────────────────────────
echo       Download and install Node.js (v22+ LTS) from:
echo         https://nodejs.org
echo.
echo       The LTS (Long Term Support) version is
echo       recommended. It includes npm automatically.
echo.
echo       After installing, restart this terminal and
echo       run setup.bat again.
echo.
set /a ERRORS=ERRORS+1

:node_done
echo.

REM ============================================
REM  npm detection
REM ============================================
echo   [3] npm — Node.js package manager
echo       Minimum version: v9 (Vite 8 requirement)
echo       Checking npm...

for /f "tokens=1" %%v in ('npm --version 2^>nul') do set NPM_VER=%%v
if not defined NPM_VER goto :npm_missing
for /f "tokens=1 delims=." %%m in ("!NPM_VER!") do set NPM_MAJOR=%%m
if !NPM_MAJOR! lss 9 goto :npm_old
echo       [OK]  npm v!NPM_VER!
goto :npm_done

:npm_old
echo       >>> FAIL : npm v!NPM_VER! is too old — need v9 or later
echo.
echo       WHAT YOU NEED TO DO:
echo       ─────────────────────────────────────────────
echo       Run this command to upgrade npm:
echo.
echo         npm install -g npm@latest
echo.
echo       Then run setup.bat again.
echo.
set /a ERRORS=ERRORS+1
goto :npm_done

:npm_missing
echo       >>> MISSING : npm not found
echo.
echo       npm comes bundled with Node.js. If Node.js is
echo       installed but npm is missing, reinstall Node.js
echo       from https://nodejs.org
echo.
set /a ERRORS=ERRORS+1

:npm_done
echo.

REM ============================================
REM  ripgrep detection
REM ============================================
echo   [4] ripgrep (rg) — fast file search (recommended)
echo       Checking ripgrep...

where rg >nul 2>nul
if %errorlevel% equ 0 goto :rg_ok
echo       >>> WARN : ripgrep not found — attempting auto-install via winget...
echo       Running: winget install BurntSushi.ripgrep.MSVC
winget install BurntSushi.ripgrep.MSVC --accept-package-agreements --accept-source-agreements -q 2>nul
if !errorlevel! equ 0 goto :rg_install_ok
echo       >>> WARN : Could not auto-install ripgrep.
echo       Without ripgrep, file search will be slower.
echo.
echo       To install manually, run in a NEW terminal:
echo         winget install BurntSushi.ripgrep.MSVC
echo.
echo       Or download from:
echo         https://github.com/BurntSushi/ripgrep/releases
goto :rg_done
:rg_install_ok
echo       [OK]  ripgrep installed successfully
goto :rg_done
:rg_ok
echo       [OK]  ripgrep found
:rg_done
echo.

REM ============================================
REM  Git detection
REM ============================================
echo   [5] Git — version control (optional)
echo       Checking Git...

where git >nul 2>nul
if %errorlevel% equ 0 goto :git_ok
echo       >>> WARN : git not found — attempting auto-install via winget...
echo       Running: winget install Git.Git
winget install Git.Git --accept-package-agreements --accept-source-agreements -q 2>nul
if !errorlevel! equ 0 goto :git_install_ok
echo       >>> WARN : Could not auto-install git.
echo       Some agent tools that clone repos won't work.
echo.
echo       To install manually, run in a NEW terminal:
echo         winget install Git.Git
echo.
echo       Or download from:
echo         https://git-scm.com/download/win
goto :git_done
:git_install_ok
echo       [OK]  git installed successfully
goto :git_done
:git_ok
echo       [OK]  git found
:git_done
echo.

REM ============================================
REM  Prerequisite summary
REM ============================================
echo   ────────────────────────────────────────────
echo   Prerequisite check complete.
echo.
if !ERRORS! gtr 0 goto :errors_fatal
echo   All required tools found. Proceeding...
echo   ────────────────────────────────────────────
goto :errors_ok

:errors_fatal
echo.
echo   ╔══════════════════════════════════════════════════════════════╗
echo   ║  !ERRORS! prerequisite(s) missing or too old.                ║
echo   ║                                                              ║
echo   ║  Please follow the instructions above to install the         ║
echo   ║  missing tools, then run setup.bat again.                    ║
echo   ╚══════════════════════════════════════════════════════════════╝
echo.
echo   Press any key to exit...
pause >nul
exit /b 1

:errors_ok
echo.

REM ============================================================================
REM  STEP 1 — Python virtual environment
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 1/7 : Python virtual environment
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   A virtual environment (venv) isolates Python packages for this
echo   project so they don't conflict with other Python projects.
echo.

if not defined PYTHON_EXE goto :venv_skip
if exist "venv\" goto :venv_exists

echo   Creating venv\ ...
echo   Command: "%PYTHON_EXE%" -m venv venv
echo.
"%PYTHON_EXE%" -m venv venv
if %errorlevel% equ 0 goto :venv_ok
echo.
echo   >>> FAIL : Could not create virtual environment.
echo.
echo   WHAT TO TRY:
echo   ─────────────────────────────────────────────
echo   1. Make sure you have write permission to this folder.
echo   2. Try running this command manually:
echo        "%PYTHON_EXE%" -m venv venv
echo   3. If that fails, reinstall Python and make sure
echo      "Add Python to PATH" is checked during install.
echo.
pause
exit /b 1
:venv_ok
echo   [OK]  Virtual environment created at venv\
goto :venv_done

:venv_exists
echo   venv\ already exists —^> skipping creation
echo   [OK]  Virtual environment ready

goto :venv_done

:venv_skip
echo   [SKIP] No Python found — cannot create virtual environment
echo   (This step requires Python to be installed.)

:venv_done
echo.

REM ============================================================================
REM  STEP 2 — Python dependencies
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 2/7 : Python dependencies
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   Installing packages listed in requirements.txt into the venv.
echo   This includes: sentence-transformers, openai, anthropic, and more.
echo.

if not exist "venv\Scripts\python.exe" goto :pip_skip

echo   Activating virtual environment...
call venv\Scripts\activate.bat

echo   Upgrading pip to latest version...
echo   Command: venv\Scripts\python.exe -m pip install --upgrade pip
echo.
venv\Scripts\python.exe -m pip install --upgrade pip -q
if %errorlevel% equ 0 goto :pip_upgrade_ok
echo   >>> WARN : pip upgrade failed — continuing anyway...
:pip_upgrade_ok

echo   Installing packages from requirements.txt...
echo   Command: venv\Scripts\pip.exe install -r requirements.txt
echo   (this may take a few minutes on first run)
echo.
venv\Scripts\pip.exe install -r requirements.txt
if %errorlevel% equ 0 goto :pip_ok

echo.
echo   >>> FAIL : pip install failed (exit code %errorlevel%).
echo.
echo   COMMON CAUSES AND FIXES:
echo   ─────────────────────────────────────────────
echo.
echo   [A] Missing Visual C++ Build Tools
echo       Some packages (like sentence-transformers) need a C++
echo       compiler on Windows. Install from:
echo         https://visualstudio.microsoft.com/visual-cpp-build-tools/
echo       During install, select:
echo         "Desktop development with C++"
echo       Then run setup.bat again.
echo.
echo   [B] Network / proxy issues
echo       If you're behind a corporate proxy, configure pip:
echo         pip config set global.proxy http://YOUR_PROXY:PORT
echo.
echo   [C] Antivirus blocking
echo       Temporarily disable real-time scanning and retry.
echo.
echo   [D] Retry manually
echo       You can retry this step alone:
echo         venv\Scripts\activate.bat
echo         pip install -r requirements.txt
echo.
pause
exit /b 1

:pip_ok
echo.
echo   [OK]  Python packages installed successfully
goto :pip_done

:pip_skip
echo   [SKIP] No venv found — cannot install Python packages
echo   (Steps 1 and 2 require Python.)

:pip_done
echo.

REM ============================================================================
REM  STEP 3 — Playwright browser
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 3/7 : Playwright Chromium browser
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   Playwright needs a real Chromium browser for web automation
echo   (web search, web browsing tools). This downloads ~150 MB.
echo.

if not exist "venv\Scripts\python.exe" goto :pw_skip

echo   Command: venv\Scripts\python.exe -m playwright install chromium --with-deps
echo   (this may take a few minutes — downloading Chromium browser)
echo.
venv\Scripts\python.exe -m playwright install chromium --with-deps 2>nul
if %errorlevel% equ 0 goto :pw_ok
echo.
echo   >>> WARN : Playwright browser install returned error code %errorlevel%.
echo   Web browsing tools will not work.
echo.
echo   WHAT TO TRY:
echo   ─────────────────────────────────────────────
echo   1. Retry manually:
echo        venv\Scripts\activate.bat
echo        python -m playwright install chromium
echo.
echo   2. If it still fails, check your internet connection
echo      or try with --with-deps flag removed.
goto :pw_done
:pw_ok
echo   [OK]  Chromium browser installed for Playwright
goto :pw_done

:pw_skip
echo   [SKIP] No venv found — cannot install Playwright browsers
echo   (Steps 1-3 require Python.)

:pw_done
echo.

REM ============================================================================
REM  STEP 4 — Node.js dependencies (Electron + renderer)
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 4/7 : Node.js dependencies (Electron + Vite)
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   Installing packages for the Electron desktop app.
echo   This includes Electron itself (~100 MB download).
echo   This is typically the longest step.
echo.

if not exist "mini_agent_electron\" goto :electron_dir_missing

cd mini_agent_electron

REM Clean up a broken node_modules from a previous failed install
if not exist "node_modules\" goto :npm_install
if exist "node_modules\electron\dist\electron.exe" goto :npm_update

echo   [CLEANUP] node_modules\ exists but Electron binary is missing.
echo            This means a previous install was interrupted.
echo            Removing broken node_modules\ ...
rmdir /s /q "node_modules" 2>nul
echo            Done — will reinstall from scratch.
echo.
goto :npm_install

:npm_update
echo   node_modules\ already exists — updating packages...
echo   Command: npm install --prefer-offline --no-audit --no-fund
echo.
call npm install --prefer-offline --no-audit --no-fund
goto :npm_check

:npm_install
echo   Installing packages (this will download Electron ~100 MB)...
echo   Command: npm install --prefer-offline --no-audit --no-fund
echo.
echo   Please wait — this may take 2-10 minutes depending on your
echo   internet speed. npm output follows:
echo   ────────────────────────────────────────────
echo.
call npm install --prefer-offline --no-audit --no-fund
echo.
echo   ────────────────────────────────────────────

:npm_check
if %errorlevel% equ 0 goto :npm_ok

echo.
echo   >>> FAIL : npm install failed (exit code %errorlevel%).
echo.
echo   ┌──────────────────────────────────────────────────────┐
echo   │  COMMON FIXES (try in order):                       │
echo   ├──────────────────────────────────────────────────────┤
echo   │                                                      │
echo   │  1. PATH TOO LONG (most common on Windows)           │
echo   │     Windows limits paths to 260 characters.          │
echo   │     Move this repo closer to the drive root:         │
echo   │       e.g. C:\mini_agent\                            │
echo   │     Then run setup.bat from the new location.        │
echo   │                                                      │
echo   │  2. CORPORATE PROXY                                  │
echo   │     Configure npm proxy:                             │
echo   │       npm config set proxy http://PROXY:PORT         │
echo   │       npm config set https-proxy http://PROXY:PORT   │
echo   │     Configure Electron mirror:                       │
echo   │       set ELECTRON_MIRROR=https://npmmirror.com/     │
echo   │                        mirrors/electron/             │
echo   │       npm install                                    │
echo   │                                                      │
echo   │  3. ANTIVIRUS BLOCKING                               │
echo   │     Disable real-time scanning temporarily           │
echo   │     and retry.                                       │
echo   │                                                      │
echo   │  4. OLD npm CACHE                                    │
echo   │     npm cache clean --force                          │
echo   │     npm install                                      │
echo   │                                                      │
echo   │  5. NODE VERSION MISMATCH                            │
echo   │     Make sure Node.js is v22+:                       │
echo   │       node --version                                 │
echo   │                                                      │
echo   │  6. RETRY FROM SCRATCH                               │
echo   │     rmdir /s /q node_modules                         │
echo   │     npm install                                      │
echo   │                                                      │
echo   └──────────────────────────────────────────────────────┘
echo.
cd ..
pause
exit /b 1

:npm_ok
echo   [OK]  npm install completed

REM Verify Electron binary exists (platform-specific binary may be missing if
REM package-lock.json was generated on macOS/Linux).
if exist "node_modules\electron\dist\electron.exe" goto :electron_found
echo.
echo   [WARN] Electron Windows binary not found.
echo          This happens when package-lock.json was created on macOS/Linux.
echo          Running: npm install electron  (to download Windows binary)
echo.
rem Remove the electron package so npm re-downloads it for the current platform
rmdir /s /q "node_modules\electron" 2>nul
call npm install electron@^42.2.0 --save
if %errorlevel% equ 0 goto :electron_force_ok
echo   >>> FAIL : npm install electron failed.
echo   Try manually:
echo     cd mini_agent_electron
echo     rmdir /s /q node_modules\electron
echo     npm install electron
cd ..
pause
exit /b 1
:electron_force_ok
rem Verify again
if exist "node_modules\electron\dist\electron.exe" goto :electron_reinstall_ok
echo   >>> FAIL : Electron binary still missing after reinstall.
echo   Try a complete clean install:
echo     cd mini_agent_electron
echo     rmdir /s /q node_modules
echo     set ELECTRON_MIRROR=https://npmmirror.com/mirrors/electron/
echo     npm install
cd ..
pause
exit /b 1
:electron_reinstall_ok
echo   [OK]  Electron Windows binary downloaded

:electron_found
echo   [OK]  Electron binary found

REM Quick smoke test
echo   Smoke-testing Electron...
node_modules\.bin\electron --version >nul 2>&1
if %errorlevel% equ 0 goto :electron_ok
echo   >>> WARN : Electron binary exists but cannot run (exit %errorlevel%).
echo   This usually means the Visual C++ Redistributable is missing.
echo.
echo   WHAT TO DO:
echo   ─────────────────────────────────────────────
echo   Download and install from:
echo     https://aka.ms/vs/17/release/vc_redist.x64.exe
echo.
echo   After installing it, run setup.bat again.
goto :electron_done
:electron_ok
for /f %%v in ('node_modules\.bin\electron --version 2^>^&1') do echo   [OK]  Electron %%v works
:electron_done
echo.

REM ============================================================================
REM  STEP 5 — Build Electron renderer
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 5/7 : Build Electron renderer (UI)
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   Building the frontend UI with Vite...
echo   Command: npm run build
echo.

call npm run build
if %errorlevel% equ 0 goto :build_ok

echo.
echo   >>> WARN : Renderer build returned error code %errorlevel%.
echo   This may be a Vite issue. The app can auto-build
echo   on first launch via "npm start".
echo.
echo   WHAT TO TRY:
echo   ─────────────────────────────────────────────
echo   1. Build manually:
echo        cd mini_agent_electron
echo        npx vite build
echo.
echo   2. Or skip the build and let npm start handle it:
echo        cd mini_agent_electron
echo        npm start
echo.
echo   3. If it keeps failing:
echo        rmdir /s /q node_modules
echo        npm cache clean --force
echo        npm install
echo        npm run build
echo.
goto :build_done

:build_ok
echo   [OK]  Renderer built —^> mini_agent_electron\renderer\dist\

:build_done
cd ..
echo.

REM ============================================================================
REM  STEP 6 — .env file check
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 6/7 : Project .env file
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   The .env file stores your API keys locally (never committed to git).
echo.

if not exist ".env" goto :env_missing
findstr /R "API_KEY=" .env >nul 2>nul
if %errorlevel% equ 0 goto :env_has_keys
echo   [INFO] .env file exists but no API_KEY= entries found.
echo          To add your keys, edit the file:
echo            notepad .env
echo.
echo          Add lines like:
echo            DEEPSEEK_API_KEY=sk-...
echo            CLAUDE_API_KEY=sk-ant-...
goto :env_done
:env_has_keys
echo   [OK]  .env file found with API key(s)
goto :env_done

:env_missing
echo   [INFO] No .env file in project root.
echo.
echo   This is optional — you can enter your API keys in the
echo   app's settings panel on first launch.
echo.
echo   Or create a .env file now:
echo     notepad .env
echo.
echo   Example content:
echo     DEEPSEEK_API_KEY=sk-...
echo     CLAUDE_API_KEY=sk-ant-...
echo     XAI_API_KEY=xai-...
echo     OLLAMA_API_KEY=ollama-...
echo     OPENAI_API_KEY=sk-...
echo.

:env_done
echo.

REM ============================================================================
REM  STEP 7 — API key environment check
REM ============================================================================
echo ──────────────────────────────────────────────────────────────────────────────
echo   STEP 7/7 : API key check
echo ──────────────────────────────────────────────────────────────────────────────
echo.
echo   Checking if any API keys are set in the current environment...
echo.

set KEY_FOUND=0
if defined DEEPSEEK_API_KEY (set KEY_FOUND=1 && echo   [OK]  DEEPSEEK_API_KEY  — environment variable set)
if defined CLAUDE_API_KEY    (set KEY_FOUND=1 && echo   [OK]  CLAUDE_API_KEY     — environment variable set)
if defined XAI_API_KEY       (set KEY_FOUND=1 && echo   [OK]  XAI_API_KEY        — environment variable set)
if defined OLLAMA_API_KEY    (set KEY_FOUND=1 && echo   [OK]  OLLAMA_API_KEY     — environment variable set)
if defined OPENAI_API_KEY    (set KEY_FOUND=1 && echo   [OK]  OPENAI_API_KEY     — environment variable set)

REM Also check %%USERPROFILE%%\.mini_agent_env (written by the app's settings panel)
if !KEY_FOUND! equ 1 goto :key_done
if not exist "%USERPROFILE%\.mini_agent_env" goto :key_missing
findstr /R "DEEPSEEK_API_KEY CLAUDE_API_KEY XAI_API_KEY OLLAMA_API_KEY OPENAI_API_KEY" "%USERPROFILE%\.mini_agent_env" >nul 2>nul
if !errorlevel! equ 1 goto :key_missing
echo   [OK]  API key found in %%USERPROFILE%%\.mini_agent_env
set KEY_FOUND=1
goto :key_done

:key_missing
echo   [INFO] No API key detected in environment or .mini_agent_env.
echo.
echo   On first launch, the app will open a settings panel where
echo   you can paste your API key. It will be saved to:
echo     %%USERPROFILE%%\.mini_agent_env
echo.
echo   Supported providers: DeepSeek, Claude (Anthropic), xAI,
echo   Ollama, OpenAI.
echo.
echo   To set a key right now in this terminal:
echo     set DEEPSEEK_API_KEY=sk-...
echo     setup.bat    (will pick it up this session)
echo.

:key_done
echo.

REM ============================================================================
REM  DONE — Summary
REM ============================================================================
echo ================================================================================
echo.
echo    Setup complete!
echo.
echo ================================================================================
echo.
echo   ┌─────────────────────────────────────────────────────────────────┐
echo   │  QUICK START                                                    │
echo   ├─────────────────────────────────────────────────────────────────┤
echo   │                                                                  │
echo   │  Launch the desktop app:                                        │
echo   │                                                                  │
echo   │    cd mini_agent_electron                                       │
echo   │    npm start                                                    │
echo   │                                                                  │
echo   │  Or, in one line from the repo root:                            │
echo   │                                                                  │
echo   │    cd mini_agent_electron ^&^& npm start                        │
echo   │                                                                  │
echo   └─────────────────────────────────────────────────────────────────┘
echo.
echo   ┌─────────────────────────────────────────────────────────────────┐
echo   │  WINDOWS TIPS                                                   │
echo   ├─────────────────────────────────────────────────────────────────┤
echo   │                                                                  │
echo   │  - Windows Defender Firewall may show a popup on first launch.  │
echo   │    Click "Allow" — Node.js needs network for the agent API.     │
echo   │                                                                  │
echo   │  - If the window appears blank/white, the renderer may not      │
echo   │    have built correctly. Run:                                   │
echo   │      cd mini_agent_electron ^&^& npm run build                  │
echo   │                                                                  │
echo   │  - Keyboard shortcuts in the chat window:                       │
echo   │      Enter        — Send message                                │
echo   │      Shift+Enter  — New line (without sending)                  │
echo   │      Escape       — Cancel streaming response                   │
echo   │                                                                  │
echo   │  - If something breaks, just run setup.bat again. It is         │
echo   │    designed to be safe to re-run.                               │
echo   │                                                                  │
echo   └─────────────────────────────────────────────────────────────────┘
echo.
endlocal
exit /b 0

REM --- Error handler for missing electron directory ---
:electron_dir_missing
echo   >>> FAIL : mini_agent_electron\ directory not found.
echo   setup.bat must be run from the mini_agent repository root.
echo.
echo   Current directory: %CD%
echo   Expected to find:  %CD%\mini_agent_electron\
echo.
echo   Make sure you cloned the full repository and are in the
echo   correct folder, then run setup.bat again.
echo.
pause
exit /b 1
