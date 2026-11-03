# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for MemoryAnalyzer.exe.
#
# Builds TWO executables into one dist/ tree:
#
#   MemoryAnalyzer.exe   -- the tile + webserver (windowed, PySide6).
#   MemoryBridge.exe     -- bridge.py pushed to a scheduled task; headless.
#
# One ``--onedir`` tree per app is bundled by Inno Setup into a single
# installer, so the user never sees PyInstaller or Python. Rationale for
# --onedir rather than --onefile: --onefile extracts the whole payload to
# %TEMP% on every launch (1-2 s delay and the dropped files sometimes trip
# corporate AV); --onedir starts instantly and is friendlier to AV.
#
# The Qt tile is a windowed app -- a console would flash on every launch --
# while the bridge is headless and uses pythonw-equivalent to avoid a
# console window when the scheduled task fires.
#
# Run from the repo root:
#   pyinstaller build-exe/MemoryAnalyzer.spec --clean

import os

block_cipher = None

# Resolve the project root from the spec's own location so a build on a
# different machine does not depend on cwd. SPECPATH is set by PyInstaller.
ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))

# Data files that must ship beside the exe: the web assets, the config
# template, and schema.sql for the first-run migration. Everything else is
# .py and is bundled by analyzing the imports.
datas = [
    (os.path.join(ROOT, 'web'),            'web'),
    (os.path.join(ROOT, 'schema.sql'),     '.'),
    (os.path.join(ROOT, 'config.example.json'), '.'),
    (os.path.join(ROOT, 'GOAL.md'),        '.'),
    (os.path.join(ROOT, 'README.md'),      '.'),
]

# Modules PyInstaller might miss. ``win32crypt`` lives inside pywin32 and is
# imported from inside a function (bridge._load_token), so static analysis
# can skip it; we name it explicitly to be sure DPAPI works in the frozen
# binary.
hiddenimports = [
    'win32crypt',
    'win32com',
    'pywintypes',
    # The sqlite dbapi is a C extension loaded lazily -- naming it stops a
    # stripped tree from losing it.
    '_sqlite3',
]

# Qt plugins we actually need. PySide6 pulls in a lot by default; the tile
# only uses the platform plugin and the imageformats, so skipping the heavy
# Qml/WebEngine trees saves ~150 MB.
qt_excludes = [
    'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtWebEngine',
    'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
    'PySide6.Qt3D', 'PySide6.QtNetworkAuth', 'PySide6.QtMultimedia',
    'PySide6.QtMultimediaWidgets', 'PySide6.QtOpenGL',
    'PySide6.QtOpenGLWidgets', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets',
    'PySide6.QtPrintSupport', 'PySide6.QtSerialPort', 'PySide6.QtSvg',
    'PySide6.QtTest',
]

# -- The tile + webserver (windowed) --------------------------------------
tile_analysis = Analysis(
    [os.path.join(ROOT, 'widget.py')],
    pathex=[ROOT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=qt_excludes,
    noarchive=False,
    cipher=block_cipher,
)
tile_pyz = PYZ(tile_analysis.pure, tile_analysis.zipped_data,
               cipher=block_cipher)
tile_exe = EXE(
    tile_pyz,
    tile_analysis.scripts,
    [],
    exclude_binaries=True,
    name='MemoryAnalyzer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,           # Windowed: no console flash on launch.
    disable_windowed_traceback=False,
    icon=None,
    version=None,
)

# -- The bridge (headless, used by the scheduled task) --------------------
bridge_analysis = Analysis(
    [os.path.join(ROOT, 'bridge.py')],
    pathex=[ROOT],
    binaries=[],
    datas=[(os.path.join(ROOT, 'schema.sql'), '.')],
    hiddenimports=['win32crypt', '_sqlite3'],
    excludes=qt_excludes,
    cipher=block_cipher,
)
bridge_pyz = PYZ(bridge_analysis.pure, bridge_analysis.zipped_data,
                 cipher=block_cipher)
bridge_exe = EXE(
    bridge_pyz,
    bridge_analysis.scripts,
    [],
    exclude_binaries=True,
    name='MemoryBridge',
    debug=False,
    console=False,           # Scheduled task runs headless.
    icon=None,
)

# One tree, both exes side by side.
coll = COLLECT(
    tile_exe, tile_analysis.binaries, tile_analysis.zipfiles,
    tile_analysis.datas,
    bridge_exe, bridge_analysis.binaries, bridge_analysis.zipfiles,
    bridge_analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='MemoryAnalyzer',
)
