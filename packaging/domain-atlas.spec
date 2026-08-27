# PyInstaller spec - builds a single-file Windows executable.
#
#   pip install pyinstaller PySide6
#   pyinstaller packaging/domain-atlas.spec
#
# The result is dist/DomainAtlas.exe, which opens the Qt interface with no
# console window.  Run it with --headless from a terminal for the CLI.

import os
import sys

block_cipher = None
project_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))

a = Analysis(
    [os.path.join(project_root, "domain_atlas.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=[
        "domainatlas.qtui",
        "domainatlas.qtui.mainwindow",
        "domainatlas.gui",
        "aiosqlite",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Qt modules the app never touches - dropping them roughly halves the build.
    excludes=[
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.Qt3DCore",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
        "PySide6.QtDesigner", "PySide6.QtTest", "tkinter", "matplotlib", "numpy",
    ],
    cipher=block_cipher,
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    name="DomainAtlas",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    # No console window: this is a desktop app.  Pass --headless to get the CLI.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(project_root, "assets", "domain-atlas.ico"),
)
