"""PyInstaller build for Domain Atlas.

    pip install pyinstaller PySide6
    pyinstaller packaging/domain-atlas.spec --noconfirm

Produces dist/DomainAtlas/ containing two executables that share one runtime:

  DomainAtlas.exe       the desktop application, no console window
  domain-atlas-cli.exe  the same program with a console, for --headless use

Windows needs both: a windowed build has no stdout, so command line output has
nowhere to go.
"""

import os

project_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(SPEC)), ".."))
icon_file = os.path.join(project_root, "assets", "domain-atlas.ico")
version_file = os.path.join(project_root, "packaging", "version_info.txt")

analysis = Analysis(
    [os.path.join(project_root, "domain_atlas.py")],
    pathex=[project_root],
    binaries=[],
    datas=[],
    hiddenimports=[
        "domainatlas.qtui",
        "domainatlas.qtui.mainwindow",
        "domainatlas.qtui.browser",
        "domainatlas.qtui.logo",
        "domainatlas.gui",
        "domainatlas.ctlog",
        "aiosqlite",
    ],
    hookspath=[],
    runtime_hooks=[],
    # Qt modules the application never touches. Dropping them roughly halves
    # the build size.
    excludes=[
        "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuick3D", "PySide6.Qt3DCore",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtMultimedia",
        "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtBluetooth",
        "PySide6.QtDesigner", "PySide6.QtTest", "PySide6.QtSql", "PySide6.QtPdf",
        "tkinter", "matplotlib", "numpy", "PIL", "pytest",
    ],
    noarchive=False,
)
archive = PYZ(analysis.pure, analysis.zipped_data)

common = dict(
    exclude_binaries=True,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    icon=icon_file if os.path.exists(icon_file) else None,
    version=version_file if os.path.exists(version_file) else None,
)

windowed = EXE(
    archive, analysis.scripts, [], name="DomainAtlas", console=False,
    disable_windowed_traceback=False, **common,
)

console = EXE(
    archive, analysis.scripts, [], name="domain-atlas-cli", console=True, **common,
)

COLLECT(
    windowed,
    console,
    analysis.binaries,
    analysis.zipfiles,
    analysis.datas,
    strip=False,
    upx=False,
    name="DomainAtlas",
)
