r"""A sandbox for the tests.

Every module here reads its paths off ``LOCALAPPDATA`` once, at import time, so
a test that wants its own database has to redirect the variable *before* the
first import and then reload. Doing that by hand in five test files is how one
of them ends up writing to the real event log, so it lives here instead.

``sandbox()`` hands back a fresh temporary root and the reloaded modules. Import
order matters: ``events`` defines ``ROOT``, and ``store`` computes ``DB_PATH``
from it, so events is reloaded first.
"""

from __future__ import annotations

import importlib
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)

if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

# Reload order is a dependency order, not alphabetical.
_MODULES = (
    "events", "store", "agent_alert", "observe", "goals", "context",
    "stats", "notify",
)


class Sandbox:
    """A temporary LOCALAPPDATA with freshly reloaded modules bound to it."""

    def __init__(self) -> None:
        self.root = tempfile.mkdtemp(prefix="smw-test-")
        self._previous = os.environ.get("LOCALAPPDATA")
        os.environ["LOCALAPPDATA"] = self.root
        self.mod: dict = {}
        for name in _MODULES:
            module = importlib.import_module(name)
            self.mod[name] = importlib.reload(module)

    def __getattr__(self, name: str):
        try:
            return self.mod[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def write(self, name: str, text: str) -> str:
        """Write a file under the sandbox root and return its path."""
        target = self.path(name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(text)
        return target

    def close(self) -> None:
        if self._previous is None:
            os.environ.pop("LOCALAPPDATA", None)
        else:
            os.environ["LOCALAPPDATA"] = self._previous
        # SQLite's WAL companions can still be held briefly on Windows, and a
        # failed cleanup is not a failed test.
        shutil.rmtree(self.root, ignore_errors=True)


def transcript(*entries: dict) -> str:
    """Serialise entries as a Claude Code transcript (one JSON object a line)."""
    import json

    return "".join(json.dumps(e) + "\n" for e in entries)


def assistant(*blocks: dict, model: str = "claude-opus-5", usage: dict | None = None,
              sidechain: bool = False, branch: str = "master") -> dict:
    """One assistant entry, shaped the way a real transcript shapes it."""
    message = {"role": "assistant", "content": list(blocks), "model": model}
    if usage is not None:
        message["usage"] = usage
    return {
        "type": "assistant", "isSidechain": sidechain, "gitBranch": branch,
        "cwd": "C:/repo", "message": message,
    }


def user(*blocks: dict, sidechain: bool = False) -> dict:
    return {
        "type": "user", "isSidechain": sidechain, "gitBranch": "master",
        "cwd": "C:/repo",
        "message": {"role": "user", "content": list(blocks)},
    }


def tool_use(name: str, tid: str, **inp) -> dict:
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def tool_result(tid: str, text: str) -> dict:
    return {"type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": text}]}


def text(body: str) -> dict:
    return {"type": "text", "text": body}
