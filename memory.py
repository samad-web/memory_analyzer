r"""Reclaim physical memory by trimming process working sets.

Nothing is ever terminated. ``EmptyWorkingSet`` asks Windows to push a
process's resident pages out of physical RAM; the process keeps running and
pages fault back in on demand. That is the only way to reduce "in use" memory
without closing anything.

The honest trade-off: pages that go out have to come back. A process you return
to afterwards will stutter briefly while it faults its pages in from the
pagefile. It is worth doing when something has ballooned and you want headroom
now; it is not a thing to run on a timer, and Windows reclaims memory on its
own under real pressure.

Deliberately not done here: purging the standby list
(``NtSetSystemInformation`` / ``MemoryPurgeStandbyList``). The standby list is
clean cache that Windows already counts as *available*, so purging it does not
move the number this widget shows -- it only throws away cache that then has to
be re-read from disk.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import psutil

PROCESS_SET_QUOTA = 0x0100
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Trimming these destabilises or badly slows the session. The kernel refuses
# most of them anyway, but skipping them keeps the attempt count honest.
PROTECTED = {
    "system",
    "system idle process",
    "registry",
    "memory compression",
    "smss.exe",
    "csrss.exe",
    "wininit.exe",
    "winlogon.exe",
    "services.exe",
    "lsass.exe",
    "dwm.exe",
}


@dataclass
class TrimResult:
    freed_mb: float
    trimmed: int
    skipped: int
    before_available_mb: float
    after_available_mb: float

    @property
    def summary(self) -> str:
        if self.freed_mb >= 1024:
            return f"freed {self.freed_mb / 1024:.1f} GB"
        if self.freed_mb >= 1:
            return f"freed {self.freed_mb:.0f} MB"
        return "nothing to free"


def _foreground_pid() -> int:
    """The process owning the active window, left alone so it stays snappy."""
    try:
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def free_memory(skip_foreground: bool = True) -> TrimResult:
    """Trim every process we are allowed to touch. Kills nothing."""
    kernel32 = ctypes.windll.kernel32
    psapi = ctypes.windll.psapi

    before = psutil.virtual_memory().available
    own_pid = ctypes.windll.kernel32.GetCurrentProcessId()
    foreground = _foreground_pid() if skip_foreground else 0

    trimmed = 0
    skipped = 0
    for proc in psutil.process_iter(["pid", "name"]):
        pid = proc.info["pid"]
        name = (proc.info["name"] or "").lower()
        if pid <= 4 or pid in (own_pid, foreground) or name in PROTECTED:
            skipped += 1
            continue

        handle = kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            skipped += 1
            continue
        try:
            if psapi.EmptyWorkingSet(handle):
                trimmed += 1
            else:
                skipped += 1
        finally:
            kernel32.CloseHandle(handle)

    after = psutil.virtual_memory().available
    return TrimResult(
        freed_mb=max(0.0, (after - before) / 1024**2),
        trimmed=trimmed,
        skipped=skipped,
        before_available_mb=before / 1024**2,
        after_available_mb=after / 1024**2,
    )
