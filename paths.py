r"""Install-vs-source root.

Every file that reads config.json, schema.sql, or the web/ assets wants the
same answer to "where is this installed". From source that is the repo root
(the directory holding widget.py); from a PyInstaller build it is the folder
holding ``MemoryAnalyzer.exe``, NOT the extracted ``_internal`` tree that
``__file__`` points at.

Centralising the choice here means a packaging change (onedir -> onefile,
or a move to a different freezer) is one edit, not three.
"""

from __future__ import annotations

import os
import sys


def install_root() -> str:
    """Directory that holds config.json, schema.sql and web/ at runtime.

    - Frozen: the directory ``sys.executable`` lives in. On a PyInstaller
      --onedir build this is the install directory the Inno Setup installer
      lays down under ``%LOCALAPPDATA%\\MemoryAnalyzer``.
    - Source: the repo root (one directory above this file is not needed;
      this file lives in the repo root alongside widget.py).
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


ROOT = install_root()
# Bundled read-only data. PyInstaller 6 lays it out in _internal/
# (sys._MEIPASS), not beside the exe; from source it is the repo root.
BUNDLE_DIR = getattr(sys, "_MEIPASS", ROOT)
CONFIG_PATH = os.path.join(ROOT, "config.json")   # user-writable: beside the exe
SCHEMA_PATH = os.path.join(BUNDLE_DIR, "schema.sql")
WEB_DIR = os.path.join(BUNDLE_DIR, "web")
