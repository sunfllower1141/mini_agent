# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for mini_agent backend server.

Produces a standalone binary that bundles Python + all dependencies,
so the Electron app can ship without requiring a Python install.

Usage:
    pyinstaller pyinstaller_backend.spec --clean --noconfirm

Output: pyinstaller_dist/mini_agent_backend  (or .exe on Windows)
"""

import os
import sys
from pathlib import Path

# ---- Paths -----------------------------------------------------------
ROOT = Path(__file__).resolve().parent  # mini_agent/
BACKEND_SCRIPT = ROOT / "mini_agent_electron" / "backend" / "server.py"
DIST_DIR = ROOT / "pyinstaller_dist"

# ---- Hidden imports ---------------------------------------------------
# PyInstaller can't trace dynamic imports.  These are imports used via
# importlib, __import__, getattr, or conditional paths in the codebase.
HIDDEN_IMPORTS = [
    # Core modules imported dynamically in server.py / agent_runtime.py
    "config",
    "llm",
    "api",
    "memory",
    "session",
    "safety",
    "prompt",
    "retry",
    "stream",
    "bootstrap",
    "interject",
    "logging_setup",
    "terminal",
    "sub_agent",
    # Tool modules (imported dynamically via tools/__init__.py)
    "tools",
    "tools._json_rpc_shared",
    "tools.agent_messages",
    "tools.agent_ops",
    "tools.agent_patterns",
    "tools.agent_todos",
    "tools.browser_ops",
    "tools.failure_learning",
    "tools.file_ops",
    "tools.lsp",
    "tools.mcp_client",
    "tools.schema",
    "tools.search_ops",
    "tools.shell_ops",
    "tools.skills",
    # Electron backend
    "emoji_svg",
    # Third-party
    "requests",
    "tomllib",
    "numpy",
    "PIL",
    "sentence_transformers",
    "textual",
    "pygments",
    "playwright",
    "prompt_toolkit",
]

# ---- Collect data files -----------------------------------------------
# Collect the emoji_svg module (it's in backend/ not the root package)
DATAS = [
    (str(ROOT / "mini_agent_electron" / "backend" / "emoji_svg.py"), "."),
]

# ---- Analysis ---------------------------------------------------------
a = Analysis(
    [str(BACKEND_SCRIPT)],
    pathex=[str(ROOT)],
    binaries=[],
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "scipy",
        "pandas",
        "IPython",
        "jupyter",
        "notebook",
        "wx",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "tkinter",
        "Tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

# ---- PYZ --------------------------------------------------------------
pyz = PYZ(a.pure, a.zipped_data, cipher=None)

# ---- EXE --------------------------------------------------------------
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="mini_agent_backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,  # strip symbols → smaller binary
    upx=True,    # UPX compress if available → much smaller
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # no console window on Windows (stdin/stdout still work)
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# ---- Clean up build dirs ----------------------------------------------
# Let the caller handle cleanup via --clean flag
