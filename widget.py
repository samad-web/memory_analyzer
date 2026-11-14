"""A frameless always-on-top desktop widget showing CPU, RAM, GPU and fan speed.

Start it through run.vbs (or run.bat) so it launches elevated -- the MSI
embedded controller refuses fan and CPU-temperature reads to an unelevated
process. The widget still runs without elevation; the fan row then shows why it
cannot read instead of a number.

Drag it anywhere, right-click for options. Position and settings persist to
config.json next to this file.
"""

from __future__ import annotations

import collections
import itertools
import json
import math
import os
import random
import subprocess
import sys
import time

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QFontMetrics,
    QCursor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

import agent_alert
import identity as identity_module
import island
import media
import sensors
import stats

HERE = os.path.dirname(os.path.abspath(__file__))
import paths  # noqa: E402

# One source of truth for where config lives, so a frozen build does not
# look inside PyInstaller's _internal tree.
CONFIG_PATH = paths.CONFIG_PATH
TASK_NAME = "SystemMonitorWidget"

# A launch while the widget is already running drops this marker; the running
# instance notices it and makes itself reachable again (see _check_summon).
# This is the escape hatch for when the tray icon is hidden and click-through
# is on, which would otherwise leave no way to open the menu.
STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "SystemMonitorWidget"
)
SUMMON_PATH = os.path.join(STATE_DIR, "summon")

# Samples kept per metric for the sparklines.
HISTORY = 48

# Hides the console window when launching helper processes.
NO_WINDOW = 0x08000000

# -- palette ---------------------------------------------------------------

# Card colour. The alpha comes from config ("bg_alpha"); 0 is a fully
# transparent widget that floats the readings straight on the wallpaper.
BG_RGB = (16, 18, 25)
BORDER = QColor(255, 255, 255, 26)
# Drawn under the text when the card is transparent, so light text stays
# readable over a pale wallpaper.
SHADOW = QColor(0, 0, 0, 190)
# Below this alpha the card cannot be relied on to back the text, so shadows
# switch on. Only the solid preset is dark enough to go without them.
SHADOW_BELOW = 220

# Agent alert banner colours.
ALERT_WAIT = QColor(255, 149, 0)      # blocked on you
ALERT_DONE = QColor(74, 222, 128)     # finished its turn
ALERT_WORK = QColor(96, 165, 250)     # running a task

# The attention animations are read off the clock rather than from a timer's
# parity bit. Two timers used to flip one shared 0/1 flag at 1Hz and at 7Hz, so
# a new alert made the banner and its dots snap between two alphas several
# times a second -- which looks like the tile glitching, not like it breathing.
# A cosine between two bounds, sampled whenever a frame happens to be painted,
# is independent of who asked for the repaint.
SPIN_PERIOD = 1.1           # seconds per rotation of a working session's ring
PULSE_PERIOD = 2.4          # seconds for one full breathe of a waiting banner
FLASH_SECONDS = 2.8         # how long the ring glows after an alert arrives
FLASH_PERIOD = 0.9          # seconds per cycle of the ring inside that window
ANIM_INTERVAL_MS = 16       # 60fps, and only while something is animating
# A session already shown as blocked should not re-pop the tile for every
# follow-up notification in the same wait; permission prompts can arrive in a
# run. A genuinely new state still announces itself immediately.
REANNOUNCE_SECONDS = 30.0
GRIP_GREEN = QColor(52, 199, 89)      # macOS-style drag handle, top-left
TEXT = QColor(230, 234, 242)
DIM = QColor(130, 140, 158)
HOT = QColor(248, 113, 113)
WARM = QColor(251, 191, 36)

ACCENTS = {
    "CPU": QColor(76, 194, 255),
    "RAM": QColor(167, 139, 250),
    "GPU": QColor(74, 222, 128),
    "FAN": QColor(251, 191, 36),
}

# Geometry in logical pixels; Qt applies DPI scaling on top of this.
WIDTH = 300
PAD = 14
# Right edge of the primary value column, and left edge of the sparkline. The
# sparkline's width is computed per row from the secondary text, which varies.
VALUE_RIGHT = PAD + 92
SPARK_LEFT = PAD + 100
SPARK_MIN_W = 30
HEADER_H = 32
ROW_H = 33
ROWS = ("CPU", "RAM", "GPU", "FAN")
# A one-line "today" productivity summary sits between the metric rows and the
# agent alert strip.
STATS_ROW_H = 22
BASE_HEIGHT = HEADER_H + ROW_H * len(ROWS) + STATS_ROW_H + PAD

# One strip per alerting project, appended under the metrics. Capped so a dozen
# idle projects cannot grow the widget off the screen; the rest are counted in
# a trailing "+N more" line.
ALERT_ROW_H = 19
# Counts display rows, which includes a header line per multi-session project.
MAX_ALERT_ROWS = 7

# The whole tile scales uniformly between these bounds; everything is laid out
# in the logical pixels above and the painter scales it at draw time.
SCALE_MIN = 0.6
SCALE_MAX = 2.5

# Shortest gap between two automatic trims. Pages pushed out fault straight
# back in, so firing on every sample over the threshold would thrash the
# pagefile and stutter whatever is running; one trim then a wait is the point.
AUTO_TRIM_COOLDOWN = 120.0

DEFAULTS = {
    "x": None,
    "y": None,
    "interval_ms": 1000,
    "slow_every": 2,
    "opacity": 1.0,
    "bg_alpha": 190,
    "hide_from_capture": True,
    "click_through": True,
    "pop_on_alert": True,
    "show_stats": True,
    "fan_mode": "rpm",
    "fan_max_rpm": 6000,
    "scale": 1.0,
    "locked": False,
    # The Dynamic Island view: a compact pill of session rings that expands
    # into a clickable dropdown, with the system's now-playing media under it.
    # On by default for the team build -- the island is the shipped face.
    "island": True,
    # Locked: the island hangs fixed top-centre of every screen (and mirrors
    # onto both). Unlocked: one draggable island whose position persists.
    "island_locked": True,
    "island_cx": None,
    "island_y": None,
    # Water-break reminder: every N minutes, block every screen for a few
    # seconds, then ask whether they drank. On by default (see MEMORY).
    "hydration_enabled": True,
    "hydration_minutes": 75,
    "hydration_block_seconds": 15,
    # Trim automatically once memory passes either limit (percent of total, or
    # gigabytes in use) -- whichever is reached first.
    "auto_trim": True,
    "auto_trim_pct": 90,
    "auto_trim_gb": 15.0,
    # The web dashboard, served from this process. Off by default: it is a
    # second way into the same data, and a port nobody asked for should not be
    # listening. "web_bind" wider than localhost forces a token (see
    # webserver.py).
    "web_enabled": False,
    "web_port": 8765,
    "web_bind": "127.0.0.1",
    "web_token": "",
    # Upload to the team project on this cadence, when team.json says to.
    "sync_enabled": True,
    "sync_seconds": 60,
    # SirahOS WorkLog bridge. Off by default: nothing goes out until the user
    # fills in a URL and signs in. ``folder_project_map`` is user-maintained
    # (see bridge.py).
    # Pre-set for the team build: teammates only need to open the dashboard
    # and sign in; URL and the obvious folder->project map are already there.
    "sirahos": {
        "api_url": "https://api.os.sirahagents.com",
        "last_signed_in_as": "",
        "folder_project_map": {},
        "share_prompts": True,
    },
}


def clamp_scale(value) -> float:
    try:
        return max(SCALE_MIN, min(SCALE_MAX, float(value)))
    except (TypeError, ValueError):
        return 1.0


def load_config() -> dict:
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            stored = json.load(fh)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
    except OSError:
        pass


class Sampler(QThread):
    """Polls the sensors off the UI thread so the widget never stutters.

    nvidia-smi is a subprocess and the EC reads are ACPI calls, both of which
    can block for tens of milliseconds. Only every ``slow_every``-th tick reads
    them; the cheap psutil counters are read on every tick.
    """

    sampled = Signal(object)
    status = Signal(str)

    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self._cfg = cfg
        self._stop = False

    def run(self) -> None:
        # Built here, not in __init__, so COM is initialised on this thread.
        reader = sensors.SensorReader(fan_mode=self._cfg["fan_mode"])
        self.status.emit(reader.fan_status)
        tick = 0
        while not self._stop:
            reader.fan_mode = self._cfg["fan_mode"]
            slow = tick % max(1, int(self._cfg["slow_every"])) == 0
            try:
                self.sampled.emit(reader.read(include_slow=slow))
            except Exception:  # keep the widget alive through a transient failure
                pass
            tick += 1
            self.msleep(max(250, int(self._cfg["interval_ms"])))

    def stop(self) -> None:
        self._stop = True


class MemoryTrimmer(QThread):
    """Runs the working-set trim off the UI thread -- it takes about a second."""

    done = Signal(object)

    def run(self) -> None:
        import memory

        try:
            self.done.emit(memory.free_memory())
        except Exception:  # noqa: BLE001 - reported in the header, not raised
            self.done.emit(None)


class SyncThread(QThread):
    """Uploads this laptop's activity to the team project, off the UI thread.

    The hooks have already written everything to disk, so this thread is pure
    catch-up: a late upload costs nothing and a failed one is retried next
    tick. It sleeps in short slices rather than one long one so quitting the
    widget does not wait out a whole interval.

    The latest sensor sample is handed over through ``submit_sample`` instead
    of being read again here -- the sampler already has it, and reading the
    embedded controller twice a minute for no reason is what this project
    exists to avoid.
    """

    reported = Signal(dict)

    def __init__(self, interval_seconds: int = 60) -> None:
        super().__init__()
        self.interval = max(20, int(interval_seconds))
        self._stop = False
        self._sample = None

    def submit_sample(self, sample) -> None:
        self._sample = sample

    def run(self) -> None:
        import sync as sync_module

        # A first push shortly after launch, so a laptop that was off all night
        # shows up on the board without waiting out a full interval.
        waited = self.interval - 8
        while not self._stop:
            if waited >= self.interval:
                waited = 0
                try:
                    if identity_module.enrolled():
                        self.reported.emit(sync_module.push(sample=self._sample))
                except Exception:  # noqa: BLE001 - a sync must never kill the tile
                    pass
            self.msleep(500)
            waited += 0.5

    def stop(self) -> None:
        self._stop = True


class MonitorWidget(QWidget):
    # Emitted when something off the UI thread -- the web dashboard's request
    # handler -- wants a setting changed. Queued by Qt onto the UI thread.
    setting_requested = Signal(str, object)

    def __init__(self) -> None:
        super().__init__()
        self.cfg = load_config()
        # With no system tray there is nothing to carry the menu while
        # click-through is on, which would leave the widget uncontrollable.
        # Fall back to a right-clickable widget in that case.
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.cfg["click_through"] = False
        self.sample = sensors.Sample()
        self.fan_status = "starting"
        self.history = {
            name: collections.deque([0.0] * HISTORY, maxlen=HISTORY) for name in ROWS
        }
        self._drag_offset: QPoint | None = None
        self._primed = False
        self._flash = ""
        self._trimmer: MemoryTrimmer | None = None
        self._alerts: list[agent_alert.Alert] = []
        # Display rows: ("group", [alerts]) | ("agent", alert) | ("solo", alert)
        self._rows: list[tuple] = []
        self._hidden_rows = 0
        self._alert_stamp: tuple = ()
        self._flash_until = 0.0
        # Soonest moment a live alert can age out. Until then the mailbox
        # cannot change on its own, so there is nothing to re-read.
        self._next_expiry = float("inf")
        # The first poll adopts whatever is already in the mailbox instead of
        # announcing it: restarting the tile should not pop and flash for
        # alerts the user already dealt with.
        self._alerts_primed = False
        self._stats_summary: dict | None = None
        self._scorecard: ScorecardWindow | None = None
        self._dashboard = None
        # Dynamic Island state. Expanded when hovered or when a session wants
        # attention; the media watcher reads the system's current track.
        self._island_expanded = False   # target: is the dropdown open
        self._island_minimized = False  # user pinned it collapsed
        self._island_anim_t = 0.0        # 0..1 drop progress (eased)
        self._island_anim = None         # the running QVariantAnimation
        self._island_hover = False
        self._media: media.MediaWatcher | None = None
        self._now: media.NowPlaying | None = None
        self._art_pixmap: tuple | None = None  # (id(bytes), QPixmap) cache
        self._logo_cache: dict[str, QPixmap | None] = {}  # project root -> logo
        self._island_clones: list = []  # one mirror window per extra screen
        self._blocker = None             # the active water-break overlay, if any
        # Declared before the sampler starts, so a sample arriving early finds
        # the attribute rather than an AttributeError.
        self._syncer: SyncThread | None = None
        self._web: object | None = None
        self.setting_requested.connect(self._on_setting_requested)
        self._hover_grip = False
        self._hover_ram = False
        self._hover_resize = False
        self._resizing = False
        self._last_auto_trim = 0.0
        self._auto_trim_pending = False
        self.scale = clamp_scale(self.cfg.get("scale", 1.0))
        self._logical_h = BASE_HEIGHT

        self.setWindowTitle("System Monitor")
        # Tool keeps it out of the taskbar and Alt-Tab, FramelessWindowHint
        # removes the title bar, and WA_TranslucentBackground lets the rounded
        # corners show the desktop through.
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self._apply_size()
        self.setWindowOpacity(float(self.cfg["opacity"]))
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_menu)

        self._place()
        self._build_tray()

        self.sampler = Sampler(self.cfg)
        self.sampler.sampled.connect(self._on_sample)
        self.sampler.status.connect(self._on_status)
        self.sampler.start()

        # Re-assert always-on-top: fullscreen apps and Explorer restarts can
        # push other windows above a Qt::Tool window.
        self._raise_timer = QTimer(self)
        self._raise_timer.timeout.connect(self._keep_on_top)
        self._raise_timer.start(5000)

        # Watch the agent mailbox. Polling an mtime is cheap and survives the
        # file being created, replaced or deleted, which a watcher on a
        # not-yet-existing file does not.
        self._alert_timer = QTimer(self)
        self._alert_timer.timeout.connect(self._poll_alert)
        self._alert_timer.start(1000)

        # Drives both attention animations: the ring that glows after a new
        # alert, and the slow breathe of a banner waiting on the user. One
        # timer for both, running only while there is something to animate, so
        # the two effects cannot fight over the frame rate.
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_anim)

        # When click-through is on, the project rows still need to be clickable
        # so they can bring a session forward. This makes just the alert strip
        # catch the mouse while the cursor is over it, leaving the rest of the
        # widget transparent to clicks.
        self._is_transparent = bool(self.cfg["click_through"])
        self._hover_timer = QTimer(self)
        self._hover_timer.timeout.connect(self._update_hover_region)
        self._hover_timer.start(150)

        # Recompute the "today" summary from the activity log every few seconds.
        # It reads a small file, so this cadence is far cheaper than per-frame.
        self._stats_timer = QTimer(self)
        self._stats_timer.timeout.connect(self._refresh_stats)
        self._stats_timer.start(5000)
        self._refresh_stats()

        # Report to the team project, if this laptop is enrolled. Runs on its
        # own thread: it is an HTTP round-trip that can sit on a dead network
        # for the full timeout, and the tile must keep painting through that.
        self._start_sync()

        if self.cfg.get("web_enabled"):
            self._start_web()

        # The island shows now-playing media, so its watcher runs only in that
        # mode. It self-disables if the winrt packages are missing.
        if self.cfg.get("island"):
            self._start_media()
            self._island_place_center()
            self._build_island_clones()

        # Water-break reminder.
        self._hydration_timer = QTimer(self)
        self._hydration_timer.timeout.connect(self._hydration_due)
        self._start_hydration()

    # -- water break ---------------------------------------------------------

    def _start_hydration(self) -> None:
        self._hydration_timer.stop()
        if not self.cfg.get("hydration_enabled", True):
            return
        try:
            minutes = max(1, int(self.cfg.get("hydration_minutes", 75)))
        except (TypeError, ValueError):
            minutes = 75
        self._hydration_timer.start(minutes * 60 * 1000)

    def _hydration_due(self) -> None:
        if self._blocker is not None:   # one at a time
            return
        try:
            secs = max(3, int(self.cfg.get("hydration_block_seconds", 15)))
        except (TypeError, ValueError):
            secs = 15
        self._blocker = HydrationBlocker(secs)
        self._blocker.finished.connect(self._on_hydration_done)
        self._blocker.begin()

    def _on_hydration_done(self) -> None:
        self._blocker = None

    def _set_hydration(self, checked: bool) -> None:
        self.cfg["hydration_enabled"] = bool(checked)
        save_config(self.cfg)
        self._start_hydration()

    # -- size ----------------------------------------------------------------

    def _apply_size(self) -> None:
        """Set the real window size from the logical layout and the scale."""
        if self.cfg.get("island"):
            w, h = self._island_logical_size()
        else:
            w, h = WIDTH, self._logical_h
        self.setFixedSize(max(1, round(w * self.scale)), max(1, round(h * self.scale)))

    def _to_logical(self, point: QPoint) -> QPoint:
        """Map a physical widget coordinate back into layout coordinates.

        Everything is laid out and hit-tested in logical pixels; the painter
        applies the scale, so incoming mouse positions have to be divided by it.
        """
        s = self.scale or 1.0
        return QPoint(round(point.x() / s), round(point.y() / s))

    def _set_scale(self, value: float, save: bool = True) -> None:
        self.scale = clamp_scale(value)
        self._apply_size()
        if save:
            self.cfg["scale"] = self.scale
            save_config(self.cfg)
        self.update()

    # -- placement -----------------------------------------------------------

    def _place(self) -> None:
        """Restore the saved position, or default to the primary screen.

        The saved position is kept only when it still lands fully inside one of
        the attached screens, so unplugging the monitor it was parked on brings
        it back to the laptop panel instead of stranding it off-screen.
        """
        x, y = self.cfg["x"], self.cfg["y"]
        if x is not None and y is not None:
            spot = QRect(int(x), int(y), self.width(), self.height())
            # Use the screen the saved position overlaps most, then pull the
            # widget fully inside it. A position a few pixels past an edge is a
            # deliberate choice worth keeping, not a reason to jump back to the
            # default corner; only a position on no screen at all is discarded.
            best = None
            best_overlap = 0
            for screen in QApplication.screens():
                area = screen.availableGeometry()
                shared = area.intersected(spot)
                covered = shared.width() * shared.height()
                if covered > best_overlap:
                    best = area
                    best_overlap = covered
            if best is not None:
                self.move(
                    max(best.left(), min(spot.x(), best.right() - self.width() + 1)),
                    max(best.top(), min(spot.y(), best.bottom() - self.height() + 1)),
                )
                return

        area = QApplication.primaryScreen().availableGeometry()
        self.move(area.right() - self.width() - 24, area.top() + 24)

    # -- agent alerts --------------------------------------------------------

    def _poll_alert(self) -> None:
        """Pick up new alerts, and let expiring ones drop off on their own."""
        self._check_summon()
        stamp = agent_alert.stamp()
        if stamp != self._alert_stamp:
            previous = {a.key: a for a in self._alerts}
            self._adopt(agent_alert.read_all(), stamp)
            if self._alerts_primed:
                for alert in self._alerts:
                    if self._worth_announcing(alert, previous.get(alert.key)):
                        self._announce(alert)
            self._alerts_primed = True
            return

        # Nothing was written, so the only thing that can have changed is an
        # entry ageing out -- and that cannot happen before the soonest TTL.
        # Re-reading on every tick otherwise meant listing and parsing the
        # whole mailbox, and repainting a translucent always-on-top window,
        # once a second for no new information.
        if self._alerts and time.time() >= self._next_expiry:
            self._adopt(agent_alert.read_all(), stamp)

    def _adopt(self, alerts: list, stamp: tuple) -> None:
        """Take a freshly read mailbox as the current state and redraw.

        ``stamp`` is the signature taken *before* the read: a hook writing
        while the directory is being walked would otherwise fall inside a
        stamp taken afterwards but outside the list just read, and go
        unnoticed. The cost is that reaping an expired file leaves a
        difference behind, so the next tick re-reads once -- announcing
        nothing, since no entry's timestamp has moved.
        """
        self._alerts = alerts
        self._alert_stamp = stamp
        self._next_expiry = min(
            (a.expires_at for a in alerts), default=float("inf")
        )
        self._resize_for_alerts()
        self._sync_anim()
        self.update()

    @staticmethod
    def _worth_announcing(alert: agent_alert.Alert, seen) -> bool:
        """Whether this entry should pop a balloon and raise the tile.

        Only attention states announce, and only on a genuine change, so a
        session merely working stays quiet and a waiting one does not re-pop
        every time its hook fires again inside the same wait.
        """
        if not alert.attention:
            return False
        if seen is None:
            return True
        if seen.at == alert.at:
            return False
        if seen.kind == alert.kind and (alert.at - seen.at) < REANNOUNCE_SECONDS:
            return False
        return True

    def _rebuild_rows(self) -> None:
        """Turn the flat alert list into display rows, grouped by project.

        A project running two or more sessions gets a header line with its name
        and a count, followed by one indented line per session -- so concurrent
        agents on one repo read as a block instead of as unrelated rows. A
        project with a single session stays a single line, as before.
        """
        rows: list[tuple] = []
        for _project, sessions in itertools.groupby(
            self._alerts, key=lambda a: a.project_key
        ):
            sessions = list(sessions)
            if len(sessions) > 1:
                rows.append(("group", sessions))
                rows.extend(("agent", alert) for alert in sessions)
            else:
                rows.append(("solo", sessions[0]))
        self._rows = rows[:MAX_ALERT_ROWS]
        self._hidden_rows = max(0, len(rows) - MAX_ALERT_ROWS)

    def _resize_for_alerts(self) -> None:
        self._rebuild_rows()
        if self.cfg.get("island"):
            self._set_island_open(self._island_wants_expand())
            self._apply_size()
            self._reposition_island()
            self._refresh_island_clones()
            return
        rows = len(self._rows) + (1 if self._hidden_rows else 0)
        height = BASE_HEIGHT + (rows * ALERT_ROW_H + 4 if rows else 0)
        if height != self._logical_h:
            self._logical_h = height
            self._apply_size()

    def _announce(self, alert: agent_alert.Alert) -> None:
        """Raise a tray balloon too, in case the widget is off to one side."""
        # A genuinely new alert un-minimises the island so it can pop open.
        self._island_minimized = False
        title = (
            f"{alert.source}: needs you"
            if alert.needs_input
            else f"{alert.source}: task done"
        )
        icon = (
            QSystemTrayIcon.MessageIcon.Warning
            if alert.needs_input
            else QSystemTrayIcon.MessageIcon.Information
        )
        try:
            self.tray.showMessage(title, alert.message or "", icon, 6000)
        except Exception:  # noqa: BLE001 - balloons are a nicety, not required
            pass
        if self.cfg["pop_on_alert"]:
            self._pop_to_front()

    def _pop_to_front(self) -> None:
        """Bring the widget above other windows and flash it, without stealing
        keyboard focus so the user's current typing is never interrupted."""
        bring_to_front(self)
        # Glow for a few seconds; the painter rings the card while this is in
        # the future. needs_input keeps breathing on its own afterwards.
        self._flash_until = time.monotonic() + FLASH_SECONDS
        self._sync_anim()
        self.update()

    def _check_summon(self) -> None:
        """Respond to a second launch asking to be made reachable again."""
        try:
            if not os.path.exists(SUMMON_PATH):
                return
            os.remove(SUMMON_PATH)
        except OSError:
            return
        self._summon()

    def _summon(self) -> None:
        """Recover control: turn click-through off, pop forward, re-show tray.

        Triggered by launching the shortcut again. This is the way back in when
        the tray icon is hidden in the overflow and click-through has left the
        widget unclickable.
        """
        if self.cfg.get("click_through"):
            self._set_click_through(False)
        try:
            self.tray.show()
        except Exception:  # noqa: BLE001
            pass
        self._pop_to_front()
        self._show_flash("menu unlocked — right-click me")

    # -- attention animation -------------------------------------------------

    def _sync_anim(self) -> None:
        """Run the animation timer only while there is something to animate."""
        want = (
            any(a.needs_input for a in self._alerts)
            or time.monotonic() < self._flash_until
            # The island's working rings rotate, so it animates while any
            # session is on a task and the tile is on screen.
            or (
                self.cfg.get("island")
                and self.isVisible()
                and any(a.kind == agent_alert.WORKING for a in self._alerts)
            )
        )
        if want and not self._anim_timer.isActive():
            self._anim_timer.start(ANIM_INTERVAL_MS)
        elif not want and self._anim_timer.isActive():
            self._anim_timer.stop()

    def _tick_anim(self) -> None:
        # Stopping before the repaint, so the frame that ends the glow is the
        # one that paints the card without it.
        self._sync_anim()
        self.update()

    def _pulse_level(self) -> float:
        """0..1 breathe for a session waiting on the user."""
        phase = (time.monotonic() % PULSE_PERIOD) / PULSE_PERIOD
        return 0.5 - 0.5 * math.cos(2 * math.pi * phase)

    def _flash_level(self) -> float:
        """0..1 glow for the ring, fading out as its window closes.

        The amplitude is scaled by the time remaining, so the ring eases away
        instead of vanishing mid-blink: a bright outline that cuts out on a
        hard edge is the part that reads as a fault rather than an effect.
        """
        left = self._flash_until - time.monotonic()
        if left <= 0:
            return 0.0
        fade = min(1.0, left / FLASH_SECONDS)
        phase = (left % FLASH_PERIOD) / FLASH_PERIOD
        return fade * (0.5 - 0.5 * math.cos(2 * math.pi * phase))

    def _spin_angle(self) -> float:
        """Current rotation in degrees of a working ring's arc."""
        phase = (time.monotonic() % SPIN_PERIOD) / SPIN_PERIOD
        return phase * 360.0

    def focus_agent(self, alert: agent_alert.Alert) -> None:
        """Bring the clicked session's own window to the front.

        The hook records which window each session runs in, so two agents in
        the same project raise their own windows rather than both resolving to
        the project folder. Only when that handle is missing or stale does this
        fall back to matching the project name against window titles.
        """
        try:
            own = int(self.winId())
        except (TypeError, ValueError):
            own = 0
        if alert.hwnd and alert.hwnd != own and focus_window(alert.hwnd):
            return
        if not bring_agent_to_front(alert.source, own):
            # No match -- say so in the header flash slot rather than silently.
            self._show_flash(f"no window for {alert.source}")

    def dismiss_alert(self, key: str = "") -> None:
        if key:
            agent_alert.clear(key)
        else:
            # Keep "working" rows: they describe reality, not something the
            # user has acknowledged. A specific key still removes anything.
            agent_alert.clear_attention()
        stamp = agent_alert.stamp()
        self._adopt(agent_alert.read_all(), stamp)

    def _paint_alert(self, p: QPainter) -> None:
        """Banner across the header, summarising every waiting project."""
        attention = [a for a in self._alerts if a.attention]
        waiting = [a for a in attention if a.needs_input]
        lead = waiting[0] if waiting else attention[0]
        colour = ALERT_WAIT if waiting else ALERT_DONE

        # Breathing fill, so a blocked agent catches the eye in peripheral
        # vision. The two bounds are close together on purpose: the movement
        # is what draws attention, and a wide swing just reads as flickering.
        fill = QColor(colour)
        fill.setAlpha(38 + int(34 * self._pulse_level()) if waiting else 34)
        band = QPainterPath()
        band.addRoundedRect(QRectF(PAD - 5, 6, WIDTH - 2 * PAD + 10, 21), 6, 6)
        p.fillPath(band, fill)

        headline = lead.headline
        if len(attention) > 1:
            headline += f" ({len(attention)})"
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.Bold))
        self._draw_text(p, PAD + 18, 21, headline, colour)

        detail = lead.message or lead.source
        p.setFont(QFont("Consolas", 8))
        room = WIDTH - PAD - (PAD + 96)
        detail = QFontMetrics(p.font()).elidedText(detail, Qt.ElideRight, room)
        self._draw_right(p, detail, WIDTH - PAD, 21, TEXT)

    def _paint_alert_rows(self, p: QPainter, top: int) -> None:
        """The session board: one line per session, grouped under its project."""
        for i, (kind, payload) in enumerate(self._rows):
            baseline = top + i * ALERT_ROW_H + 13
            if kind == "group":
                self._paint_group_header(p, payload, baseline)
            else:
                self._paint_session_row(p, payload, baseline, nested=kind == "agent")

        if self._hidden_rows:
            baseline = top + len(self._rows) * ALERT_ROW_H + 13
            p.setFont(QFont("Consolas", 8))
            self._draw_text(p, PAD + 13, baseline, f"+{self._hidden_rows} more", DIM)

    def _paint_group_header(self, p: QPainter, sessions: list, baseline: int) -> None:
        """Project name and session count, above that project's agent rows."""
        # The header takes the colour of the group's most urgent session, so a
        # project with someone blocked in it is obvious without reading the rows.
        if any(a.needs_input for a in sessions):
            colour = ALERT_WAIT
        elif any(a.kind == agent_alert.DONE for a in sessions):
            colour = ALERT_DONE
        else:
            colour = ALERT_WORK

        name_font = QFont("Segoe UI", 8, QFont.Weight.Bold)
        p.setFont(name_font)
        name = sessions[0].source[:20]
        self._draw_text(p, PAD, baseline, name, colour)
        name_w = QFontMetrics(name_font).horizontalAdvance(name)

        p.setFont(QFont("Consolas", 8))
        self._draw_text(
            p, PAD + name_w + 7, baseline, f"{len(sessions)} agents", DIM
        )

    def _paint_session_row(
        self, p: QPainter, alert: agent_alert.Alert, baseline: int, nested: bool
    ) -> None:
        colour = {
            agent_alert.NEEDS_INPUT: ALERT_WAIT,
            agent_alert.WORKING: ALERT_WORK,
        }.get(alert.kind, ALERT_DONE)

        # Nested rows sit under their project header, so they are indented and
        # labelled by session instead of repeating the project name.
        left = PAD + (12 if nested else 0)
        if nested:
            # A faint elbow tying the row to its group header above.
            p.setPen(QPen(QColor(255, 255, 255, 28), 1))
            p.drawLine(PAD + 4, baseline - 9, PAD + 4, baseline - 3)
            p.drawLine(PAD + 4, baseline - 3, PAD + 9, baseline - 3)

        dot = QColor(colour)
        if alert.needs_input:
            # Breathes between two clearly visible levels. Halving its alpha on
            # alternate ticks made the row look like it was dropping frames.
            dot.setAlpha(190 + int(65 * self._pulse_level()))
        p.setBrush(dot)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(left, baseline - 7, 6, 6))

        name_font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        p.setFont(name_font)
        label = f"#{alert.session_tag}" if nested else alert.source[:18]
        self._draw_text(p, left + 13, baseline, label, TEXT if not nested else DIM)

        # Right side: what it wants, or how long the result has been there. It
        # gets only the room the label leaves, so a long name shortens the note
        # instead of colliding with it.
        p.setFont(QFont("Consolas", 8))
        note = alert.age_text if alert.kind == agent_alert.DONE else (
            alert.message or alert.age_text
        )
        label_right = left + 13 + QFontMetrics(name_font).horizontalAdvance(label)
        room = (WIDTH - PAD) - (label_right + 10)
        note = QFontMetrics(p.font()).elidedText(note, Qt.ElideRight, max(0, room))
        self._draw_right(p, note, WIDTH - PAD, baseline, colour)

    # -- Dynamic Island ------------------------------------------------------

    ISLAND_MEDIA_H = 54        # expanded media strip height (logical px)
    ISLAND_MEDIA_ART = 38
    ISLAND_MEDIA_BTN = 26
    ISLAND_SYS_H = 28          # the memory-analysis readout row, when expanded
    ISLAND_FLARE = 13          # how far the top corners flare out past the body

    def _island_rows(self) -> tuple[list, int]:
        """Sessions listed in the dropdown (capped), and how many are hidden."""
        shown = self._alerts[:MAX_ALERT_ROWS]
        return shown, max(0, len(self._alerts) - len(shown))

    def _island_wants_expand(self) -> bool:
        """Open the dropdown when hovered, or whenever a session wants you --
        unless the user has minimised it, which pins it shut until they click
        the pill (a brand-new alert un-minimises it, see _announce)."""
        if self._island_minimized:
            return False
        attention = any(a.attention for a in self._alerts)
        has_content = bool(self._alerts) or self._now is not None
        return attention or (self._island_hover and has_content)

    def _island_rows_block_h(self) -> int:
        rows, hidden = self._island_rows()
        n_rows = len(rows) + (1 if hidden else 0)
        return island.rows_height(n_rows)

    def _island_sys_top(self) -> int:
        """Top of the memory-analysis row (under the session rows)."""
        return island.PILL_H + self._island_rows_block_h()

    def _island_media_top(self) -> int:
        return self._island_sys_top() + self.ISLAND_SYS_H

    def _island_collapsed_size(self) -> tuple[int, int]:
        n = len(self._alerts)
        w, h = island.pill_size(n)
        if n == 0 and self._now is not None:
            w = max(w, 150)   # room for art + title + a play/pause button
        return w, h

    def _island_expanded_size(self) -> tuple[int, int]:
        n = len(self._alerts)
        h = self._island_media_top()
        if self._now is not None:
            h += self.ISLAND_MEDIA_H
        return island.panel_width(n), h

    def _island_logical_size(self) -> tuple[int, int]:
        """The WINDOW size. It stays at the expanded extent and never changes
        during a drop -- the drop is painted inside a fixed window, which is
        what keeps it smooth (resizing a translucent top window per frame is
        what tore). It carries FLARE of slack each side so the top corners can
        flare out past the card without being clipped."""
        ew, eh = self._island_expanded_size()
        cw, ch = self._island_collapsed_size()
        return max(cw, ew) + 2 * self.ISLAND_FLARE, max(ch, eh)

    def _island_card_size(self) -> tuple[float, float]:
        """The visible card, eased from the collapsed pill to the open panel.
        Only the painting changes between frames; the window does not."""
        cw, ch = self._island_collapsed_size()
        t = self._island_anim_t
        if t <= 0.001:
            return float(cw), float(ch)
        ew, eh = self._island_expanded_size()
        return cw + (ew - cw) * t, ch + (eh - ch) * t

    def _island_card_left(self) -> float:
        """x of the card inside the (wider) window, so it stays centred."""
        winw, _ = self._island_logical_size()
        cardw, _ = self._island_card_size()
        return (winw - cardw) / 2.0

    def _island_card_rect_global(self, win) -> QRect:
        """The card's on-screen rectangle for ``win`` (the tile or a clone),
        used for hover and region click-through."""
        s = self.scale or 1.0
        left = self._island_card_left()
        cardw, cardh = self._island_card_size()
        g = win.geometry()
        return QRect(
            int(g.x() + left * s), int(g.y()), int(cardw * s), int(cardh * s)
        )

    @staticmethod
    def _island_card_path(w: float, h: float, flare: float) -> QPainterPath:
        """A card that hangs from the ceiling: the top flares OUT past the body
        (widest where it meets the screen edge) through concave curves down to
        the body sides, with convex rounded bottoms -- a water drop spreading
        where it clings to the top. The flare depth sits inside the pill's top
        padding so content never pokes out of it."""
        f = min(flare, w / 3)
        neck = 11.0              # flare depth (within the pill's top padding)
        br = min(20.0, w / 2, max(1.0, h - neck))
        p = QPainterPath()
        p.moveTo(-f, 0)
        p.lineTo(w + f, 0)                                      # flared top edge
        p.quadTo(w, 0, w, neck)                                 # concave curve in, right
        p.lineTo(w, h - br)
        p.arcTo(QRectF(w - 2 * br, h - 2 * br, 2 * br, 2 * br), 0, -90)    # convex BR
        p.lineTo(br, h)
        p.arcTo(QRectF(0, h - 2 * br, 2 * br, 2 * br), 270, -90)          # convex BL
        p.lineTo(0, neck)
        p.quadTo(0, 0, -f, 0)                                   # concave curve in, left
        p.closeSubpath()
        return p

    def _paint_island(self, p: QPainter) -> None:
        w, h = self._island_card_size()   # the animated card, not the window
        t = self._island_anim_t
        p.save()
        p.setRenderHint(QPainter.SmoothPixmapTransform)   # crisp scaled logos
        p.translate(self._island_card_left(), 0)   # keep the card centred

        f = self.ISLAND_FLARE
        card = self._island_card_path(w, h, f)
        # Premium painted glass. (A real OS backdrop-blur of the desktop is not
        # attainable for a frameless translucent window on current Windows --
        # the acrylic/blur APIs render it solid or clear, verified -- so this is
        # a carefully layered smoked-glass look, not true refraction.)
        alpha = 228
        body = QLinearGradient(0, 0, 0, h)
        body.setColorAt(0.0, QColor(60, 67, 88, alpha))     # lit top
        body.setColorAt(0.30, QColor(30, 34, 47, alpha))
        body.setColorAt(1.0, QColor(13, 15, 22, alpha))     # deep base
        p.fillPath(card, body)
        p.save()
        p.setClipPath(card)
        # Specular sheen along the top edge -- light catching the glass.
        sheen = QLinearGradient(0, 0, 0, 24)
        sheen.setColorAt(0.0, QColor(255, 255, 255, 115))
        sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
        p.fillRect(QRectF(-f, 0, w + 2 * f, 24), sheen)
        # A cool vibrancy glow pooling toward the bottom, for depth.
        glow = QLinearGradient(0, h * 0.45, 0, h)
        glow.setColorAt(0.0, QColor(96, 128, 180, 0))
        glow.setColorAt(1.0, QColor(84, 116, 168, 26))
        p.fillRect(QRectF(0, 0, w, h), glow)
        # A hairline of light just inside the top edge (the glass bevel).
        p.setPen(QPen(QColor(255, 255, 255, 60), 1))
        p.drawLine(int(14), 1, int(w - 14), 1)
        p.restore()
        # Outer rim: bright, the specular edge of the glass.
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(255, 255, 255, 78), 1.3))
        p.drawPath(card)

        # Content is clipped to the card, so a half-dropped row is cut by the
        # card edge (a clean reveal) rather than drawn past it.
        p.setClipRect(QRectF(0, 0, w, h))
        n = len(self._alerts)
        if n:
            self._paint_island_rings(p, n)
        elif self._now is not None and t <= 0.5:
            self._paint_island_media_mini(p, w)
        else:
            self._paint_island_idle(p, w)

        if t > 0.02:
            self._paint_island_rows(p, w)
            self._paint_island_sys(p, w, self._island_sys_top())
            if self._now is not None:
                self._paint_island_media(p, w, self._island_media_top())

        if t > 0.9:
            self._paint_island_minimize(p, w)
            self._paint_island_close(p, w)
            self._paint_island_grip(p, w, h)
        p.restore()

    def _island_close_center(self, w: float) -> tuple[float, float]:
        return w - 13, 13

    def _island_minimize_center(self, w: float) -> tuple[float, float]:
        return w - 36, 13

    def _paint_island_minimize(self, p: QPainter, w: float) -> None:
        """A minus to the left of the x: tuck the dropdown back to the pill."""
        cx, cy = self._island_minimize_center(w)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 24))
        p.drawEllipse(QRectF(cx - 9, cy - 9, 18, 18))
        p.setPen(QPen(QColor(230, 236, 245, 230), 1.6))
        p.drawLine(int(cx - 4), int(cy), int(cx + 4), int(cy))

    def _paint_island_close(self, p: QPainter, w: float) -> None:
        """A small red x in the top-right that quits the tile."""
        cx, cy = self._island_close_center(w)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 24))
        p.drawEllipse(QRectF(cx - 9, cy - 9, 18, 18))
        p.setPen(QPen(QColor(255, 107, 99, 230), 1.6))   # reference danger red
        p.drawLine(int(cx - 3), int(cy - 3), int(cx + 3), int(cy + 3))
        p.drawLine(int(cx - 3), int(cy + 3), int(cx + 3), int(cy - 3))

    def _paint_island_grip(self, p: QPainter, w: float, h: float) -> None:
        """Three ticks in the bottom-right corner: drag to resize the island."""
        col = TEXT if self._resizing else DIM
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(col, 1.3))
        bx, by = w - 6, h - 6
        for off in (1, 5, 9):
            p.drawLine(int(bx - off), int(by), int(bx), int(by - off))

    def _over_island_resize(self, local: QPoint) -> bool:
        if self._island_anim_t < 0.9:
            return False
        cardw, cardh = self._island_card_size()
        x = local.x() - self._island_card_left()
        return (cardw - 16) <= x <= cardw and (cardh - 16) <= local.y() <= cardh

    # -- drop animation ------------------------------------------------------

    def _set_island_open(self, open_: bool) -> None:
        if open_ == self._island_expanded:
            return
        self._island_expanded = open_
        from PySide6.QtCore import QVariantAnimation, QEasingCurve

        if self._island_anim is not None:
            self._island_anim.stop()
        anim = QVariantAnimation(self)
        anim.setStartValue(float(self._island_anim_t))
        anim.setEndValue(1.0 if open_ else 0.0)
        anim.setDuration(240)
        anim.setEasingCurve(QEasingCurve.OutCubic if open_ else QEasingCurve.InCubic)
        anim.valueChanged.connect(self._on_drop_tick)
        anim.finished.connect(self._on_drop_done)
        self._island_anim = anim
        anim.start()

    def _on_drop_tick(self, value) -> None:
        # The window is fixed-size, so a drop frame is a repaint, not a resize.
        self._island_anim_t = float(value)
        self.update()
        for clone in self._island_clones:
            clone.update()

    def _on_drop_done(self) -> None:
        self._island_anim = None

    def _paint_island_rings(self, p: QPainter, n: int) -> None:
        for i, (x, y, d) in enumerate(island.ring_rects(n)):
            self._paint_ring(p, self._alerts[i], x, y, d)
        badge = island.badge_pos(n)
        if badge:
            bx, by, _bw = badge
            p.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
            self._draw_text(p, bx, by + island.RING_D // 2 + 5,
                            f"+{island.overflow(n)}", DIM)

    def _ring_arc(self, alert: agent_alert.Alert) -> tuple[QColor, float, float]:
        """(colour, span_deg, start_deg) for a session's ring."""
        if alert.kind == agent_alert.WORKING:
            return QColor(ALERT_WORK), -270.0, -self._spin_angle()
        if alert.needs_input:
            c = QColor(ALERT_WAIT)
            c.setAlpha(170 + int(85 * self._pulse_level()))
            return c, 360.0, 90.0
        return QColor(ALERT_DONE), 360.0, 90.0

    def _project_logo(self, alert: agent_alert.Alert):
        """The project's own logo as a QPixmap, or None -- cached per project."""
        root = agent_alert.project_root(alert.cwd or "")
        if root in self._logo_cache:
            return self._logo_cache[root]
        pm = None
        path = island.logo_path(root)
        if path:
            loaded = QPixmap(path)
            pm = loaded if not loaded.isNull() else None
        self._logo_cache[root] = pm
        return pm

    def _paint_ring(
        self, p: QPainter, alert: agent_alert.Alert, x: float, y: float, d: float
    ) -> None:
        cx, cy = x + d / 2, y + d / 2
        disc_r = d / 2 - 4
        disc_rect = QRectF(cx - disc_r, cy - disc_r, 2 * disc_r, 2 * disc_r)

        logo = self._project_logo(alert)
        if logo is not None:
            # The project's real icon, clipped to the disc.
            clip = QPainterPath()
            clip.addEllipse(disc_rect)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(disc_rect.toRect(), logo)
            p.restore()
        else:
            # No icon in the project: a coloured disc with its initials.
            p.setPen(Qt.NoPen)
            p.setBrush(QColor.fromHsv(island.hue(alert.project_key), 130, 165))
            p.drawEllipse(disc_rect)
            p.setPen(QColor(255, 255, 255, 235))
            p.setFont(QFont("Segoe UI", max(6, int(disc_r * 0.75)), QFont.Weight.Bold))
            p.drawText(disc_rect, Qt.AlignCenter, island.glyph(alert.source))

        colour, span, start = self._ring_arc(alert)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(colour, 3, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(x + 1.5, y + 1.5, d - 3, d - 3),
                  int(start * 16), int(span * 16))

    def _paint_island_idle(self, p: QPainter, w: float) -> None:
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 45))
        cy = island.PILL_H / 2
        p.drawEllipse(QRectF(w / 2 - 4, cy - 4, 8, 8))

    def _paint_island_rows(self, p: QPainter, w: float) -> None:
        rows, hidden = self._island_rows()
        top = island.rows_top()
        for i, alert in enumerate(rows):
            self._paint_island_row(p, alert, top + i * island.ROW_H, w)
        if hidden:
            y = top + len(rows) * island.ROW_H + 13
            p.setFont(QFont("Consolas", 8))
            self._draw_text(p, island.PILL_PAD + 2, y, f"+{hidden} more", DIM)

    def _paint_island_row(
        self, p: QPainter, alert: agent_alert.Alert, y: float, w: float
    ) -> None:
        d = island.SMALL_RING_D
        x = island.PILL_PAD
        cy = y + island.ROW_H / 2
        self._paint_ring(p, alert, x, cy - d / 2, d)

        name_font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        p.setFont(name_font)
        name = alert.source[:22]
        self._draw_text(p, int(x + d + 8), int(cy) + 4, name, TEXT)

        colour = {
            agent_alert.NEEDS_INPUT: ALERT_WAIT,
            agent_alert.WORKING: ALERT_WORK,
        }.get(alert.kind, ALERT_DONE)
        note = alert.age_text if alert.kind == agent_alert.DONE else (
            alert.message or alert.age_text
        )
        p.setFont(QFont("Consolas", 8))
        name_w = QFontMetrics(name_font).horizontalAdvance(name)
        room = (w - island.PILL_PAD) - (x + d + 8 + name_w + 10)
        note = QFontMetrics(p.font()).elidedText(note, Qt.ElideRight, max(0, int(room)))
        self._draw_right(p, note, w - island.PILL_PAD, int(cy) + 4, colour)

    def _paint_island_sys(self, p: QPainter, w: float, top: float) -> None:
        """The memory-analysis readout: CPU / RAM / GPU, with a free-RAM button.

        The island carries the monitor's core numbers so it is not only an
        agent board -- this is the same sample the tile already reads."""
        pad = island.PILL_PAD
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawLine(int(pad), int(top), int(w - pad), int(top))
        cy = top + self.ISLAND_SYS_H / 2
        s = self.sample
        gpu = f"{s.gpu_pct:.0f}%" if s.gpu_pct is not None else "--"
        text = f"CPU {s.cpu_pct:.0f}%    RAM {s.ram_pct:.0f}%    GPU {gpu}"
        p.setFont(QFont("Consolas", 8))
        self._draw_text(p, int(pad), int(cy) + 4, text, DIM)
        self._paint_island_freebtn(p, w - pad - 11, cy)

    def _paint_island_freebtn(self, p: QPainter, cx: float, cy: float) -> None:
        accent = ACCENTS["RAM"]
        bg = QColor(accent)
        bg.setAlpha(70)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawEllipse(QRectF(cx - 10, cy - 10, 20, 20))
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(accent, 1.4))
        p.drawLine(int(cx), int(cy - 4), int(cx), int(cy + 2))        # stem
        p.drawLine(int(cx - 3), int(cy - 1), int(cx), int(cy + 2))    # head
        p.drawLine(int(cx + 3), int(cy - 1), int(cx), int(cy + 2))
        p.drawLine(int(cx - 4), int(cy + 5), int(cx + 4), int(cy + 5))  # tray

    # -- media strip ---------------------------------------------------------

    def _art_pixmap_for(self):
        """QPixmap of the current album art, cached by the art bytes' identity."""
        now = self._now
        if now is None or not now.art:
            return None
        key = id(now.art)
        if self._art_pixmap and self._art_pixmap[0] == key:
            return self._art_pixmap[1]
        pm = QPixmap()
        pm = pm if pm.loadFromData(now.art) else None
        self._art_pixmap = (key, pm)
        return pm

    def _paint_art(self, p: QPainter, x: float, y: float, d: float, circle: bool) -> None:
        path = QPainterPath()
        if circle:
            path.addEllipse(QRectF(x, y, d, d))
        else:
            path.addRoundedRect(QRectF(x, y, d, d), 6, 6)
        pm = self._art_pixmap_for()
        if pm is not None:
            p.save()
            p.setClipPath(path)
            p.drawPixmap(QRectF(x, y, d, d).toRect(), pm)
            p.restore()
            return
        # No art: a flat chip with a small note glyph.
        p.setPen(Qt.NoPen)
        p.fillPath(path, QColor(255, 255, 255, 30))
        cx, cy = x + d / 2, y + d / 2
        p.setPen(QPen(DIM, 1.4))
        p.setBrush(DIM)
        p.drawEllipse(QRectF(cx - 5, cy + 1, 3.5, 3.5))
        p.setBrush(Qt.NoBrush)
        p.drawLine(int(cx - 1.5), int(cy + 3), int(cx - 1.5), int(cy - 5))
        p.drawLine(int(cx - 1.5), int(cy - 5), int(cx + 4), int(cy - 6))
        p.drawLine(int(cx + 4), int(cy - 6), int(cx + 4), int(cy + 1))

    def _media_button_xs(self, w: float) -> tuple[float, float, float]:
        """x-centres of prev, play/pause, next in the expanded strip."""
        pad = island.PILL_PAD
        btn = self.ISLAND_MEDIA_BTN
        nx = w - pad - btn / 2
        px = nx - btn - 6
        pv = px - btn - 6
        return pv, px, nx

    def _paint_island_media(self, p: QPainter, w: float, top: float) -> None:
        now = self._now
        pad = island.PILL_PAD
        p.setPen(QPen(QColor(255, 255, 255, 22), 1))
        p.drawLine(int(pad), int(top), int(w - pad), int(top))

        art = self.ISLAND_MEDIA_ART
        ay = top + (self.ISLAND_MEDIA_H - art) / 2
        self._paint_art(p, pad, ay, art, circle=False)

        cy = top + self.ISLAND_MEDIA_H / 2
        pv, px, nx = self._media_button_xs(w)
        self._paint_prev_glyph(p, pv, cy, now.can_prev)
        self._paint_play_glyph(p, px, cy, now.playing, now.can_play_pause)
        self._paint_next_glyph(p, nx, cy, now.can_next)

        tx = pad + art + 10
        troom = (pv - self.ISLAND_MEDIA_BTN / 2 - 8) - tx
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        title = QFontMetrics(p.font()).elidedText(
            now.title or "Playing", Qt.ElideRight, max(10, int(troom))
        )
        self._draw_text(p, int(tx), int(cy) - 2, title, TEXT)
        if now.artist:
            p.setFont(QFont("Consolas", 8))
            artist = QFontMetrics(p.font()).elidedText(
                now.artist, Qt.ElideRight, max(10, int(troom))
            )
            self._draw_text(p, int(tx), int(cy) + 12, artist, DIM)

    def _paint_island_media_mini(self, p: QPainter, w: float) -> None:
        now = self._now
        pad = island.PILL_PAD
        d = island.RING_D
        cy = island.PILL_H / 2
        self._paint_art(p, pad, (island.PILL_H - d) / 2, d, circle=True)
        bx = w - pad - 12
        self._paint_play_glyph(p, bx, cy, now.playing, now.can_play_pause)
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        tx = pad + d + 8
        room = (bx - 16) - tx
        title = QFontMetrics(p.font()).elidedText(
            now.title or "Playing", Qt.ElideRight, max(10, int(room))
        )
        self._draw_text(p, int(tx), int(cy) + 4, title, TEXT)

    def _paint_play_glyph(
        self, p: QPainter, cx: float, cy: float, playing: bool, enabled: bool = True
    ) -> None:
        col = TEXT if enabled else DIM
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 28))
        p.drawEllipse(QRectF(cx - 13, cy - 13, 26, 26))
        p.setBrush(col)
        if playing:
            p.drawRect(QRectF(cx - 4.5, cy - 6, 3.2, 12))
            p.drawRect(QRectF(cx + 1.3, cy - 6, 3.2, 12))
        else:
            tri = QPainterPath()
            tri.moveTo(cx - 4, cy - 6)
            tri.lineTo(cx - 4, cy + 6)
            tri.lineTo(cx + 6, cy)
            tri.closeSubpath()
            p.fillPath(tri, col)

    def _paint_prev_glyph(self, p: QPainter, cx: float, cy: float, enabled: bool) -> None:
        col = TEXT if enabled else DIM
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 20))
        p.drawEllipse(QRectF(cx - 13, cy - 13, 26, 26))
        tri = QPainterPath()
        tri.moveTo(cx + 4, cy - 5)
        tri.lineTo(cx + 4, cy + 5)
        tri.lineTo(cx - 3, cy)
        tri.closeSubpath()
        p.fillPath(tri, col)
        p.setPen(QPen(col, 2))
        p.drawLine(int(cx - 5), int(cy - 5), int(cx - 5), int(cy + 5))

    def _paint_next_glyph(self, p: QPainter, cx: float, cy: float, enabled: bool) -> None:
        col = TEXT if enabled else DIM
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 20))
        p.drawEllipse(QRectF(cx - 13, cy - 13, 26, 26))
        tri = QPainterPath()
        tri.moveTo(cx - 4, cy - 5)
        tri.lineTo(cx - 4, cy + 5)
        tri.lineTo(cx + 3, cy)
        tri.closeSubpath()
        p.fillPath(tri, col)
        p.setPen(QPen(col, 2))
        p.drawLine(int(cx + 5), int(cy - 5), int(cx + 5), int(cy + 5))

    # -- island interaction --------------------------------------------------

    @staticmethod
    def _hit_circle(x: float, y: float, cx: float, cy: float, r: float) -> bool:
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    def _island_media_hit(self, x: float, y: float):
        """The media control under a point, as a zero-arg callable, or None."""
        if self._media is None or self._now is None:
            return None
        now = self._now
        if self._island_expanded:
            top = self._island_media_top()
            if y < top:
                return None
            w, _ = self._island_card_size()
            cy = top + self.ISLAND_MEDIA_H / 2
            r = self.ISLAND_MEDIA_BTN / 2
            pv, px, nx = self._media_button_xs(w)
            if now.can_prev and self._hit_circle(x, y, pv, cy, r):
                return self._media.previous
            if now.can_play_pause and self._hit_circle(x, y, px, cy, r):
                return self._media.toggle_play_pause
            if now.can_next and self._hit_circle(x, y, nx, cy, r):
                return self._media.next
            return None
        # Collapsed media mini only exists when no sessions are shown.
        if self._alerts or not now.can_play_pause:
            return None
        w, _ = self._island_card_size()
        if self._hit_circle(x, y, w - island.PILL_PAD - 12, island.PILL_H / 2, 14):
            return self._media.toggle_play_pause
        return None

    def _island_click(self, local: QPoint) -> bool:
        """Handle a left-click in island mode. True if it was consumed."""
        cardw, _ = self._island_card_size()
        x = local.x() - self._island_card_left()   # into card space
        y = local.y()
        if self._island_anim_t > 0.9:
            ccx, ccy = self._island_close_center(cardw)
            if self._hit_circle(x, y, ccx, ccy, 11):
                self.close()   # quit the box
                return True
            mcx, mcy = self._island_minimize_center(cardw)
            if self._hit_circle(x, y, mcx, mcy, 11):
                self._island_minimized = True   # tuck it back to the pill
                self._set_island_open(self._island_wants_expand())
                return True
        action = self._island_media_hit(x, y)
        if action is not None:
            action()
            return True
        if self._island_expanded:
            cy = self._island_sys_top() + self.ISLAND_SYS_H / 2
            if self._hit_circle(x, y, cardw - island.PILL_PAD - 11, cy, 12):
                self.free_memory()
                return True
        n = len(self._alerts)
        # Rings live in the pill strip in both states; rows only when expanded.
        if y <= island.PILL_H:
            # While minimised, a click on the pill re-opens it instead of
            # jumping to a session -- that is how you get the dropdown back.
            if self._island_minimized:
                self._island_minimized = False
                self._set_island_open(True)
                return True
            i = island.ring_at(n, x, y)
            if 0 <= i < n:
                self.focus_agent(self._alerts[i])
                return True
            return False
        if self._island_expanded:
            rows, _ = self._island_rows()
            i = island.row_at(y, len(rows))
            if 0 <= i < len(rows):
                self.focus_agent(rows[i])
                return True
        return False

    # -- media watcher -------------------------------------------------------

    def _start_media(self) -> None:
        if not media.MediaWatcher.available:
            return
        if self._media is not None and self._media.isRunning():
            return
        self._media = media.MediaWatcher()
        self._media.changed.connect(self._on_media)
        self._media.start()

    def _stop_media(self) -> None:
        if self._media is not None:
            try:
                self._media.changed.disconnect(self._on_media)
            except (TypeError, RuntimeError):
                pass
            self._media.stop()
            self._media = None
        self._now = None
        self._art_pixmap = None

    def _on_media(self, now) -> None:
        self._now = now
        self._art_pixmap = None
        if self.cfg.get("island"):
            self._set_island_open(self._island_wants_expand())
            self._apply_size()
            self._reposition_island()
            self.update()
            self._refresh_island_clones()

    def _set_island(self, checked: bool) -> None:
        self.cfg["island"] = bool(checked)
        save_config(self.cfg)
        if checked:
            self._start_media()
        else:
            if self._island_anim is not None:
                self._island_anim.stop()
                self._island_anim = None
            self._island_expanded = False
            self._island_anim_t = 0.0
            self._stop_media()
        self._apply_size()
        if checked:
            self._island_place_center()
            self._build_island_clones()
        else:
            self._teardown_island_clones()
            self._place()
        self._sync_anim()
        self.update()

    def _build_island_clones(self) -> None:
        """A mirror island on every screen other than this window's own, so the
        island is present on both monitors. Clones hold no state -- they paint
        this tile's island and forward clicks back to it."""
        self._teardown_island_clones()
        # Mirrors only make sense for the fixed, top-centre island.
        if not self.cfg.get("island") or not self.cfg.get("island_locked", True):
            return
        own = self.screen()
        for screen in QApplication.screens():
            if screen is own:
                continue
            clone = IslandClone(self, screen)
            clone.show()
            self._island_clones.append(clone)

    def _teardown_island_clones(self) -> None:
        for clone in self._island_clones:
            clone.close()
        self._island_clones = []

    def _refresh_island_clones(self) -> None:
        for clone in self._island_clones:
            clone.sync()

    def _island_place_center(self) -> None:
        """Hang the island from the top-centre of its current screen."""
        self._reposition_island()

    def _reposition_island(self) -> None:
        """Place the island: locked -> flush top-centre of its screen; unlocked
        -> the saved centre/top so a drag sticks and it still grows downward."""
        screen = self.screen() or QApplication.primaryScreen()
        area = screen.geometry()
        cx = self.cfg.get("island_cx")
        top = self.cfg.get("island_y")
        if self.cfg.get("island_locked", True) or cx is None or top is None:
            self.move(area.center().x() - self.width() // 2, area.top())
        else:
            self.move(int(cx) - self.width() // 2, int(top))

    def _set_island_locked(self, checked: bool) -> None:
        self.cfg["island_locked"] = bool(checked)
        save_config(self.cfg)
        if self.cfg.get("island"):
            self._reposition_island()
            if checked:
                self._build_island_clones()   # mirrors return when fixed
            else:
                self._teardown_island_clones()  # one movable island when free
        self.update()

    # -- tray ----------------------------------------------------------------

    def _build_tray(self) -> None:
        """The tray icon is the control surface when click-through is on."""
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_tray_icon())
        self.tray.setToolTip("System Monitor")
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(lambda: self.dismiss_alert())
        self.tray.show()

    def _tray_activated(self, reason) -> None:
        # Left-click opens the same menu, since a click-through widget cannot.
        if reason == QSystemTrayIcon.ActivationReason.Trigger and any(
            a.attention for a in self._alerts
        ):
            self.dismiss_alert()
            return
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.Context,
        ):
            self._build_menu().exec(QCursor.pos())

    def _update_tray_tooltip(self, sample: sensors.Sample) -> None:
        fans = "--"
        if sample.fan_values:
            unit = "%" if sample.fan_unit == "%" else " RPM"
            fans = "/".join(f"{v:.0f}" for v in sample.fan_values) + unit
        cpu_temp = f" {sample.cpu_temp:.0f}°C" if sample.cpu_temp else ""
        gpu = f"{sample.gpu_pct:.0f}%" if sample.gpu_pct is not None else "--"
        self.tray.setToolTip(
            f"CPU {sample.cpu_pct:.0f}%{cpu_temp}\n"
            f"RAM {sample.ram_pct:.0f}%  "
            f"{sample.ram_used_gb:.1f}/{sample.ram_total_gb:.0f} GB\n"
            f"GPU {gpu}\nFan {fans}"
        )

    def _keep_on_top(self) -> None:
        if self.isVisible():
            self.raise_()

    def _set_transparent(self, transparent: bool) -> None:
        if transparent != self._is_transparent:
            set_click_through(self, transparent)
            self._is_transparent = transparent

    def _update_hover_region(self) -> None:
        """Keep the window click-through except over its interactive bits.

        With click-through off, the whole widget is clickable as normal. With it
        on, the window is transparent to clicks except over the drag grip, the
        free-RAM button and the project rows -- so those stay usable while every
        other click still passes straight through to the window underneath.
        """
        physical = self.mapFromGlobal(QCursor.pos())
        inside = self.rect().contains(physical)
        # Island mode: the whole small window is interactive, and hovering it
        # expands the dropdown. Everything passes through when the cursor is off.
        if self.cfg.get("island"):
            cur = QCursor.pos()
            # Hover is tested against the visible card, not the (larger) fixed
            # window, so the transparent margin never counts as hovering.
            over_self = self._island_card_rect_global(self).contains(cur)
            self._island_hover = over_self or any(
                self._island_card_rect_global(c).contains(cur)
                for c in self._island_clones
            )
            # Minimise holds until the cursor leaves the whole island WINDOW,
            # not just the card. Testing the card would clear it the instant the
            # collapsing card shrank out from under the pointer, and hover would
            # immediately re-open it. The window is fixed-size, so it is stable.
            over_window = self.geometry().contains(cur) or any(
                c.geometry().contains(cur) for c in self._island_clones
            )
            if not over_window:
                self._island_minimized = False
            self._set_island_open(self._island_wants_expand())
            # The window spans the expanded extent but only the card is solid,
            # so clicks pass through wherever the cursor is off the card.
            self._set_transparent(not over_self)
            if over_self:
                loc = self._to_logical(self.mapFromGlobal(cur))
                self.setCursor(
                    Qt.SizeFDiagCursor if self._over_island_resize(loc)
                    else Qt.PointingHandCursor
                )
            else:
                self.unsetCursor()
            return
        local = self._to_logical(physical)
        over_grip = inside and self._over_grip(local)
        over_ram = inside and self._over_ram_btn(local)
        over_resize = inside and self._over_resize(local)
        over_rows = (
            inside
            and bool(self._alerts)
            and (BASE_HEIGHT - PAD) <= local.y() <= self._logical_h
            and not over_resize
        )

        if self.cfg["click_through"]:
            self._set_transparent(
                not (over_grip or over_ram or over_resize or over_rows)
            )
        else:
            self._set_transparent(False)

        if over_grip:
            self.setCursor(
                Qt.ClosedHandCursor if self._drag_offset is not None else Qt.OpenHandCursor
            )
        elif over_resize:
            self.setCursor(Qt.SizeFDiagCursor)
        elif over_ram or over_rows:
            self.setCursor(Qt.PointingHandCursor)
        else:
            self.unsetCursor()

        state = (over_grip, over_ram, over_resize)
        if state != (self._hover_grip, self._hover_ram, self._hover_resize):
            self._hover_grip, self._hover_ram, self._hover_resize = state
            self.update()

    # -- sampling ------------------------------------------------------------

    def _on_status(self, status: str) -> None:
        self.fan_status = status
        self.update()

    def _on_sample(self, sample: sensors.Sample) -> None:
        self.sample = sample
        values = {
            "CPU": sample.cpu_pct,
            "RAM": sample.ram_pct,
            "GPU": sample.gpu_pct if sample.gpu_pct is not None else 0.0,
            "FAN": self._fan_fraction(sample),
        }
        for name, value in values.items():
            if self._primed:
                self.history[name].append(value)
            else:
                # Fill the backlog with the first reading; starting from zero
                # would draw a climb that never happened.
                self.history[name].extend([value] * HISTORY)
        self._primed = True
        self._update_tray_tooltip(sample)
        self._maybe_auto_trim(sample)
        # Hand the sample to the sync thread rather than letting it read the
        # sensors again -- the embedded controller is the expensive one here.
        if self._syncer is not None:
            self._syncer.submit_sample(sample)
        self.update()

    def _maybe_auto_trim(self, sample: sensors.Sample) -> None:
        """Free memory on its own once usage passes the configured limit.

        Fires on crossing either limit -- percent of total, or gigabytes in use
        -- and then holds off for AUTO_TRIM_COOLDOWN, because the pages it
        pushes out get faulted back in and usage climbs again within seconds.
        Nothing is ever terminated; this is the same working-set trim as the
        manual button.
        """
        if not self.cfg.get("auto_trim", True):
            return
        if self._trimmer is not None and self._trimmer.isRunning():
            return
        if sample.ram_total_gb <= 0:
            return  # no reading yet
        try:
            limit_pct = float(self.cfg.get("auto_trim_pct", 90))
            limit_gb = float(self.cfg.get("auto_trim_gb", 15.0))
        except (TypeError, ValueError):
            return
        if sample.ram_pct < limit_pct and sample.ram_used_gb < limit_gb:
            return
        now = time.monotonic()
        if now - self._last_auto_trim < AUTO_TRIM_COOLDOWN:
            return
        self._last_auto_trim = now
        self._auto_trim_pending = True
        self.free_memory()

    def _fan_fraction(self, sample: sensors.Sample) -> float:
        """Fan reading as 0-100, so it shares the sparkline scale."""
        if not sample.fan_values:
            return 0.0
        ceiling = 100.0 if sample.fan_unit == "%" else float(self.cfg["fan_max_rpm"])
        return 100.0 * max(sample.fan_values) / ceiling

    # -- painting ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # The native window has to exist before its capture affinity can be
        # set, so this runs on show rather than in __init__.
        set_capture_exclusion(self, bool(self.cfg["hide_from_capture"]))
        set_click_through(self, bool(self.cfg["click_through"]))

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt naming
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # Everything below is drawn in logical pixels; this is what resizes the
        # tile without reflowing any of its layout.
        p.scale(self.scale, self.scale)

        # The Dynamic Island is a wholly separate face: its own pill/panel,
        # rings and media strip, none of the monitor layout below.
        if self.cfg.get("island"):
            self._paint_island(p)
            p.end()
            return

        alpha = int(self.cfg["bg_alpha"])
        if alpha > 0:
            card = QPainterPath()
            card.addRoundedRect(
                QRectF(0.5, 0.5, WIDTH - 1, self._logical_h - 1), 14, 14
            )
            p.fillPath(card, QColor(*BG_RGB, alpha))
            # A visible border around an invisible card looks like a stray box,
            # so fade it out with the fill. NoBrush because drawPath would
            # otherwise fill the card a second time with the current brush.
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 26 * alpha // 255), 1))
            p.drawPath(card)

        if any(a.attention for a in self._alerts):
            self._paint_alert(p)
        else:
            self._paint_header(p)
        for i, name in enumerate(ROWS):
            self._paint_row(p, name, HEADER_H + i * ROW_H)
        self._paint_ram_button(p)
        self._paint_stats(p)
        self._paint_grip(p)
        self._paint_resize(p)
        if self._alerts:
            self._paint_alert_rows(p, BASE_HEIGHT - PAD + 2)

        # An outline that glows for a few seconds after a new alert pops the
        # widget forward, so the eye lands on it.
        level = self._flash_level()
        if level > 0.01:
            glow = ALERT_WAIT if any(a.needs_input for a in self._alerts) else ALERT_DONE
            glow = QColor(glow)
            glow.setAlpha(55 + int(170 * level))
            ring = QPainterPath()
            # The logical height, not self.height(): the painter is already
            # scaled, so using the physical height drew the ring a further
            # factor of "scale" too large on any tile that had been resized --
            # a coloured rectangle hanging off the card for three seconds.
            ring.addRoundedRect(
                QRectF(1.5, 1.5, WIDTH - 3, self._logical_h - 3), 13, 13
            )
            # NoBrush explicitly: drawPath fills as well as strokes, and the
            # session rows above leave the brush set to their status dot. The
            # ring was therefore flooding the whole card with that colour for
            # the length of the flash -- the tile going solid green after a
            # task finished, or solid orange when an agent asked a question.
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(glow, 2.5))
            p.drawPath(ring)
        p.end()

    # -- grip + inline buttons ----------------------------------------------

    def _grip_rect(self) -> QRect:
        """Grab zone around the top-left drag dot."""
        return QRect(PAD - 3, 5, 20, 20)

    def _over_grip(self, local: QPoint) -> bool:
        return self._grip_rect().contains(local)

    def _ram_btn_rect(self) -> QRect:
        """The one-click free-RAM button, between the RAM label and its value."""
        top = HEADER_H + ROWS.index("RAM") * ROW_H
        return QRect(PAD + 30, top + 6, 22, 22)

    def _over_ram_btn(self, local: QPoint) -> bool:
        return self._ram_btn_rect().contains(local)

    def _paint_grip(self, p: QPainter) -> None:
        """A green macOS-style dot you can grab to drag the tile anywhere,
        across monitors, even while click-through is on everywhere else."""
        rect = self._grip_rect()
        cx, cy = rect.center().x(), rect.center().y()
        if self._hover_grip or self._drag_offset is not None:
            halo = QColor(GRIP_GREEN)
            halo.setAlpha(55)
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(QRectF(cx - 8, cy - 8, 16, 16))
        p.setPen(QPen(QColor(0, 0, 0, 70), 1))
        p.setBrush(GRIP_GREEN)
        p.drawEllipse(QRectF(cx - 4.5, cy - 4.5, 9, 9))
        # A tiny highlight, so it reads as a glossy control rather than a status.
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 120))
        p.drawEllipse(QRectF(cx - 2.4, cy - 2.8, 2.2, 2.2))

    def _resize_rect(self) -> QRect:
        """Grab zone for the bottom-right resize corner (logical pixels)."""
        return QRect(WIDTH - 18, self._logical_h - 18, 18, 18)

    def _over_resize(self, local: QPoint) -> bool:
        return self._resize_rect().contains(local)

    def _paint_resize(self, p: QPainter) -> None:
        """Three diagonal ticks in the bottom-right corner, the classic grip."""
        colour = TEXT if (self._hover_resize or self._resizing) else DIM
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(colour, 1.3))
        bx, by = WIDTH - 6, self._logical_h - 6
        for off in (1, 5, 9):
            p.drawLine(bx - off, by, bx, by - off)

    def _paint_ram_button(self, p: QPainter) -> None:
        """A download-style glyph on the RAM row: one click trims working sets."""
        rect = self._ram_btn_rect()
        cx, cy = rect.center().x(), rect.center().y()
        accent = ACCENTS["RAM"]
        bg = QColor(accent)
        bg.setAlpha(80 if self._hover_ram else 36)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawEllipse(QRectF(cx - 9, cy - 9, 18, 18))
        glyph = TEXT if self._hover_ram else accent
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(glyph, 1.4))
        p.drawLine(cx, cy - 4, cx, cy + 2)          # arrow stem
        p.drawLine(cx - 3, cy - 1, cx, cy + 2)      # arrowhead
        p.drawLine(cx + 3, cy - 1, cx, cy + 2)
        p.drawLine(cx - 4, cy + 5, cx + 4, cy + 5)  # tray/baseline

    def _paint_stats(self, p: QPainter) -> None:
        """One line of today's productivity figures, under the metric rows."""
        if not self.cfg.get("show_stats", True):
            return
        top = HEADER_H + ROW_H * len(ROWS)
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        p.drawLine(PAD, top + 1, WIDTH - PAD, top + 1)

        baseline = top + 16
        label_font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        p.setFont(label_font)
        self._draw_text(p, PAD, baseline, "TODAY", DIM)
        label_w = QFontMetrics(label_font).horizontalAdvance("TODAY")

        s = self._stats_summary
        p.setFont(QFont("Consolas", 8))
        if not s or (s.get("tasks", 0) == 0 and s.get("tokens", 0) == 0):
            self._draw_right(p, "no activity yet", WIDTH - PAD, baseline, DIM)
            return
        text = f"{s['tasks']} · {s['tokens_h']} tok · {s['reply_h']}"
        room = (WIDTH - PAD) - (PAD + label_w + 10)
        text = QFontMetrics(p.font()).elidedText(text, Qt.ElideRight, max(0, room))
        self._draw_right(p, text, WIDTH - PAD, baseline, TEXT)

    def _refresh_stats(self) -> None:
        try:
            self._stats_summary = stats.today_summary()
        except Exception:  # noqa: BLE001 - a stats glitch must not kill the tile
            self._stats_summary = None
        self.update()

    def _open_scorecard(self) -> None:
        """The old single-page Scorecard, kept as a quick read-only glance."""
        if self._scorecard is None:
            self._scorecard = ScorecardWindow(self.cfg)
        self._scorecard.refresh()
        self._scorecard.show()
        self._scorecard.raise_()
        self._scorecard.activateWindow()

    # -- dashboard -----------------------------------------------------------

    def _open_dashboard(self) -> None:
        """Open (or re-focus) the full dashboard window.

        Imported here rather than at module scope so the tile keeps starting
        even if the dashboard has a problem -- it is the bigger, newer surface,
        and the tile is the thing that must not go down with it.
        """
        if self._dashboard is None:
            try:
                import dashboard as dashboard_module

                self._dashboard = dashboard_module.DashboardWindow(self.cfg, tile=self)
            except Exception as exc:  # noqa: BLE001
                self._show_flash("dashboard failed")
                self.tray.showMessage(
                    "Dashboard could not open",
                    str(exc)[:220],
                    QSystemTrayIcon.Warning,
                    6000,
                )
                return
        self._dashboard.refresh()
        self._dashboard.show()
        bring_to_front(self._dashboard)

    # -- web dashboard -------------------------------------------------------

    def _apply_setting_async(self, key: str, value) -> None:
        """Queue a setting change from a non-UI thread.

        The web server's request handlers run on their own threads, and
        touching Qt widgets from one of those is undefined behaviour that
        usually shows up as a crash much later. The signal below is connected
        with no explicit type, so Qt queues it onto the UI thread for us.
        """
        self.setting_requested.emit(key, value)

    def _on_setting_requested(self, key: str, value) -> None:
        self.apply_setting(key, value)
        if self._dashboard is not None:
            self._dashboard._load_tile_controls()

    def _start_web(self) -> str:
        if self._web is not None and getattr(self._web, "running", False):
            return self._web.url
        try:
            import webserver

            self._web = webserver.DashboardServer(
                self.cfg, tile=self, apply_hook=self._apply_setting_async
            )
            url = self._web.start()
        except Exception as exc:  # noqa: BLE001
            self._web = None
            self._show_flash("web failed")
            self.tray.showMessage(
                "Web dashboard could not start", str(exc)[:220],
                QSystemTrayIcon.Warning, 6000,
            )
            return ""
        if not url:
            self._show_flash("no free port")
        return url

    def _set_web_enabled(self, checked: bool) -> None:
        self.cfg["web_enabled"] = checked
        save_config(self.cfg)
        if checked:
            url = self._start_web()
            if url:
                self.tray.showMessage(
                    "Web dashboard running", url, QSystemTrayIcon.Information, 5000
                )
        elif self._web is not None:
            self._web.stop()
            self._web = None

    def _open_web_dashboard(self) -> None:
        url = self._start_web()
        if not url:
            return
        self.cfg["web_enabled"] = True
        save_config(self.cfg)
        import webbrowser

        webbrowser.open(url)

    # -- team sync -----------------------------------------------------------

    def _start_sync(self) -> None:
        if not self.cfg.get("sync_enabled", True):
            return
        if self._syncer is not None and self._syncer.isRunning():
            return
        self._syncer = SyncThread(int(self.cfg.get("sync_seconds", 60) or 60))
        self._syncer.start()

    def _open_deploy_tab(self) -> None:
        self._open_dashboard()
        if self._dashboard is not None:
            self._dashboard.tabs.setCurrentIndex(
                self._dashboard.tabs.count() - 1
            )

    def _set_sync_enabled(self, checked: bool) -> None:
        self.cfg["sync_enabled"] = checked
        save_config(self.cfg)
        if checked:
            self._start_sync()
        elif self._syncer is not None:
            self._syncer.stop()
            self._syncer.wait(2000)
            self._syncer = None

    def _set_show_stats(self, checked: bool) -> None:
        self.cfg["show_stats"] = checked
        save_config(self.cfg)
        self.update()

    def _paint_header(self, p: QPainter) -> None:
        font = QFont("Segoe UI", 8, QFont.Weight.DemiBold)
        font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.4)
        p.setFont(font)
        self._draw_text(p, PAD + 18, 21, "SYSTEM", DIM)

        # Status dot: green when the EC is readable, amber when it is not.
        p.setBrush(QColor(74, 222, 128) if self.fan_status == "ok" else WARM)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(WIDTH - PAD - 7, 11, 6, 6))

        if self._flash:
            p.setFont(QFont("Consolas", 8))
            self._draw_right(p, self._flash, WIDTH - PAD - 14, 21, ACCENTS["RAM"])

    def _paint_row(self, p: QPainter, name: str, top: int) -> None:
        value, secondary, warn = self._row_data(name)
        baseline = top + 20

        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self._draw_text(p, PAD, baseline, name, DIM)

        # Primary value, right-aligned so digits line up down the column.
        p.setFont(QFont("Consolas", 11, QFont.Weight.Bold))
        self._draw_right(p, value, VALUE_RIGHT, baseline, warn or TEXT)

        secondary_font = QFont("Consolas", 8)
        p.setFont(secondary_font)
        self._draw_right(p, secondary, WIDTH - PAD, baseline, DIM)

        # Give the sparkline whatever room the secondary text leaves, so a long
        # reading like "13.5/16G" pushes it back instead of overlapping it.
        secondary_w = QFontMetrics(secondary_font).horizontalAdvance(secondary)
        spark_w = (WIDTH - PAD - secondary_w - 10) - SPARK_LEFT
        if spark_w >= SPARK_MIN_W:
            self._paint_spark(p, name, ACCENTS[name], SPARK_LEFT, top + 7, spark_w, 19)

    def _draw_right(
        self, p: QPainter, text: str, right: int, baseline: int, colour: QColor
    ) -> None:
        width = QFontMetrics(p.font()).horizontalAdvance(text)
        self._draw_text(p, right - width, baseline, text, colour)

    def _draw_text(
        self, p: QPainter, x: int, baseline: int, text: str, colour: QColor
    ) -> None:
        """Draw text, shadowed when there is no card behind it to sit on."""
        if int(self.cfg["bg_alpha"]) < SHADOW_BELOW:
            p.setPen(SHADOW)
            p.drawText(x + 1, baseline + 1, text)
        p.setPen(colour)
        p.drawText(x, baseline, text)

    def _paint_spark(
        self, p: QPainter, name: str, accent: QColor, x: int, y: int, w: int, h: int
    ) -> None:
        series = list(self.history[name])
        if not series:
            return

        # A faint baseline keeps the row legible when the series sits flat at 0.
        p.setPen(QPen(QColor(255, 255, 255, 18), 1))
        p.drawLine(x, y + h, x + w, y + h)

        step = w / (len(series) - 1) if len(series) > 1 else w
        points = [
            (x + i * step, y + h - (max(0.0, min(100.0, v)) / 100.0) * h)
            for i, v in enumerate(series)
        ]

        fill = QPainterPath()
        fill.moveTo(points[0][0], y + h)
        for px, py in points:
            fill.lineTo(px, py)
        fill.lineTo(points[-1][0], y + h)
        fill.closeSubpath()

        grad = QLinearGradient(x, y, x, y + h)
        top_colour = QColor(accent)
        top_colour.setAlpha(110)
        bottom_colour = QColor(accent)
        bottom_colour.setAlpha(10)
        grad.setColorAt(0.0, top_colour)
        grad.setColorAt(1.0, bottom_colour)
        p.setPen(Qt.NoPen)
        p.fillPath(fill, grad)

        line = QPainterPath()
        line.moveTo(*points[0])
        for point in points[1:]:
            line.lineTo(*point)
        # NoBrush: drawPath fills as well as strokes, and an open path is
        # filled as though it were closed. Without this the curve flooded its
        # own row with whatever colour was painted last -- the header's status
        # dot for the CPU row, the row above's accent for the rest.
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(accent, 1.4))
        p.drawPath(line)

        # Dot marking the newest reading.
        p.setBrush(accent)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(points[-1][0] - 1.8, points[-1][1] - 1.8, 3.6, 3.6))

    def _row_data(self, name: str) -> tuple[str, str, QColor | None]:
        s = self.sample

        if name == "CPU":
            temp = f"{s.cpu_temp:.0f}°C" if s.cpu_temp else "--"
            return f"{s.cpu_pct:.0f}%", temp, _temp_colour(s.cpu_temp)

        if name == "RAM":
            used = f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.0f}G"
            return f"{s.ram_pct:.0f}%", used, None

        if name == "GPU":
            if s.gpu_pct is None:
                return "--", "no GPU", None
            parts = []
            if s.gpu_mem_used_mb is not None:
                parts.append(f"{s.gpu_mem_used_mb / 1024:.1f}G")
            if s.gpu_temp:
                parts.append(f"{s.gpu_temp:.0f}°C")
            return f"{s.gpu_pct:.0f}%", " ".join(parts), _temp_colour(s.gpu_temp)

        # FAN: two fans on this chassis, the second shown as the dim secondary.
        if not s.fan_values:
            hint = "no admin" if "administrator" in self.fan_status else "n/a"
            return "--", hint, None
        unit = "%" if s.fan_unit == "%" else ""
        primary = f"{s.fan_values[0]:.0f}{unit}"
        if len(s.fan_values) > 1:
            secondary = f"{s.fan_values[1]:.0f}{unit}"
        else:
            secondary = s.fan_unit
        return primary, secondary, None

    # -- interaction ---------------------------------------------------------

    def _alert_at(self, y: float) -> agent_alert.Alert | None:
        """The session under a y-coordinate, or None above the alert strip.

        A click on a group header resolves to that project's most urgent
        session, so clicking the header still raises something sensible.
        """
        top = BASE_HEIGHT - PAD + 2
        if y < top or not self._rows:
            return None
        row = int((y - top) // ALERT_ROW_H)
        if not 0 <= row < len(self._rows):
            return None
        kind, payload = self._rows[row]
        return payload[0] if kind == "group" else payload

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        local = self._to_logical(event.position().toPoint())
        # Island mode has its own hit map (rings, dropdown rows, media buttons);
        # an empty spot drags the pill, as with the monitor's blank area.
        if self.cfg.get("island"):
            if self._over_island_resize(local):
                self._resizing = True
                self.setCursor(Qt.SizeFDiagCursor)
                event.accept()
                return
            if self._island_click(local):
                event.accept()
                return
            # When unlocked, empty space drags the island; when locked it is
            # pinned top-centre and does not move.
            if not self.cfg.get("island_locked", True):
                origin = event.globalPosition().toPoint()
                self._drag_offset = origin - self.frameGeometry().topLeft()
                event.accept()
            return
        # Bottom-right corner scales the whole tile while dragged.
        if self._over_resize(local):
            self._resizing = True
            self.setCursor(Qt.SizeFDiagCursor)
            event.accept()
            return
        # The green grip always drags, regardless of click-through or lock --
        # it is the deliberate "pick me up" handle, incl. onto another monitor.
        if self._over_grip(local):
            origin = event.globalPosition().toPoint()
            self._drag_offset = origin - self.frameGeometry().topLeft()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return
        # One click on the RAM button frees memory, no menu, no double-click.
        if self._over_ram_btn(local):
            self.free_memory()
            event.accept()
            return
        # A click on a project row brings that session's window forward.
        alert = self._alert_at(local.y())
        if alert is not None:
            self.focus_agent(alert)
            event.accept()
            return
        if not self.cfg["locked"]:
            origin = event.globalPosition().toPoint()
            self._drag_offset = origin - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resizing and event.buttons() & Qt.LeftButton:
            dragged = event.globalPosition().toPoint().x() - self.x()
            if self.cfg.get("island"):
                # Scale against the open panel's own width, so the grip tracks
                # the cursor whatever the island currently holds.
                base = max(1, self._island_expanded_size()[0])
                self._set_scale(dragged / base, save=False)
                self._reposition_island()
                self._refresh_island_clones()
            else:
                # The top-left stays anchored; the dragged width drives the
                # scale and the height follows it proportionally.
                self._set_scale(dragged / WIDTH, save=False)
            event.accept()
            return
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_offset)
            event.accept()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        """Double-clicking the RAM row frees memory."""
        if event.button() != Qt.LeftButton:
            return
        local = self._to_logical(event.position().toPoint())
        row = int((local.y() - HEADER_H) // ROW_H)
        if 0 <= row < len(ROWS) and ROWS[row] == "RAM":
            self._drag_offset = None
            self.free_memory()
            event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._resizing:
            self._resizing = False
            self.cfg["scale"] = self.scale
            save_config(self.cfg)
            self.unsetCursor()
            self.update()
            self._refresh_island_clones()
            return
        if self._drag_offset is not None:
            self._drag_offset = None
            if self.cfg.get("island"):
                # Remember where the free island was dropped (centre-x, top-y).
                self.cfg["island_cx"] = self.x() + self.width() // 2
                self.cfg["island_y"] = self.y()
            else:
                self.cfg["x"], self.cfg["y"] = self.x(), self.y()
            save_config(self.cfg)
            if not self.cfg.get("island") and self._over_grip(event.position().toPoint()):
                self.setCursor(Qt.OpenHandCursor)
            else:
                self.unsetCursor()

    def _show_menu(self, pos: QPoint) -> None:
        self._build_menu().exec(self.mapToGlobal(pos))

    def _build_menu(self) -> QMenu:
        """The options menu, shared by the widget and the tray icon.

        When click-through is on the widget cannot be right-clicked at all, so
        the tray icon is the only way in -- both must offer the same menu.
        """
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #181b24; color: #e6eaf2;"
            " border: 1px solid #2a2f3a; padding: 4px; }"
            "QMenu::item { padding: 5px 24px 5px 20px; border-radius: 4px; }"
            "QMenu::item:selected { background: #2b3242; }"
            "QMenu::separator { height: 1px; background: #2a2f3a; margin: 4px 8px; }"
        )

        heading = menu.addAction(
            "Fan sensor: MSI EC (ok)"
            if self.fan_status == "ok"
            else f"Fan sensor: {self.fan_status}"
        )
        heading.setEnabled(False)
        menu.addSeparator()

        dashboard = menu.addAction("Dashboard…")
        dashboard.setToolTip(
            "Every task, the tokens it burnt, the team board, and the tile's "
            "own settings"
        )
        dashboard.triggered.connect(self._open_dashboard)

        web = menu.addAction("Open web dashboard")
        web.setToolTip(
            "The same dashboard in a browser, served from this process on "
            "localhost"
        )
        web.triggered.connect(self._open_web_dashboard)

        scorecard = menu.addAction("Scorecard (quick view)…")
        scorecard.setToolTip("The one-page summary, read-only")
        scorecard.triggered.connect(self._open_scorecard)
        menu.addSeparator()

        if self._alerts:
            agents = menu.addMenu(f"Agents ({len(self._alerts)})")
            shared = collections.Counter(a.project_key for a in self._alerts)
            for alert in self._alerts:
                marks = {agent_alert.NEEDS_INPUT: "!", agent_alert.WORKING: "~"}
                mark = marks.get(alert.kind, "ok")
                label = alert.source[:24]
                # Several sessions in one project need telling apart here too.
                if shared[alert.project_key] > 1:
                    label = f"{alert.source[:18]} #{alert.session_tag}"
                sub = agents.addMenu(f"[{mark}] {label}")
                front = sub.addAction("Bring to front")
                front.triggered.connect(
                    lambda _checked=False, a=alert: self.focus_agent(a)
                )
                drop = sub.addAction("Dismiss")
                drop.triggered.connect(
                    lambda _checked=False, k=alert.key: self.dismiss_alert(k)
                )
            agents.addSeparator()
            agents.addAction("Dismiss all").triggered.connect(
                lambda _checked=False: self.dismiss_alert()
            )
            menu.addSeparator()

        trim = menu.addAction("Free memory")
        trim.setToolTip("Trim process working sets. Nothing is closed.")
        trim.triggered.connect(self.free_memory)

        auto = menu.addAction("Auto-free when full")
        auto.setCheckable(True)
        auto.setChecked(bool(self.cfg.get("auto_trim", True)))
        auto.setToolTip(
            f"Trim automatically past {int(self.cfg.get('auto_trim_pct', 90))}% "
            f"or {float(self.cfg.get('auto_trim_gb', 15.0)):.0f} GB, "
            f"at most every {int(AUTO_TRIM_COOLDOWN / 60)} min"
        )
        auto.triggered.connect(self._set_auto_trim)

        self._radio_menu(
            menu.addMenu("Auto-free at"),
            [("80%", 80), ("85%", 85), ("90%", 90), ("95%", 95)],
            int(self.cfg.get("auto_trim_pct", 90)),
            self._set_auto_trim_pct,
        )
        menu.addSeparator()

        self._radio_menu(
            menu.addMenu("Update rate"),
            [("0.5 s", 500), ("1 s", 1000), ("2 s", 2000), ("5 s", 5000)],
            self.cfg["interval_ms"],
            self._set_interval,
        )
        self._radio_menu(
            menu.addMenu("Size"),
            [("Small", 0.8), ("Normal", 1.0), ("Large", 1.3), ("Huge", 1.7)],
            round(float(self.scale), 2),
            self._set_scale,
        )
        self._radio_menu(
            menu.addMenu("Opacity"),
            [("60%", 0.6), ("75%", 0.75), ("90%", 0.9), ("Opaque", 1.0)],
            float(self.cfg["opacity"]),
            self._set_opacity,
        )
        self._radio_menu(
            menu.addMenu("Background"),
            [("Transparent", 0), ("Tinted", 190), ("Solid", 252)],
            int(self.cfg["bg_alpha"]),
            self._set_bg_alpha,
        )
        self._radio_menu(
            menu.addMenu("Fan readout"),
            [("RPM", "rpm"), ("Duty %", "duty")],
            self.cfg["fan_mode"],
            self._set_fan_mode,
        )

        through = menu.addAction("Click-through")
        through.setCheckable(True)
        through.setChecked(bool(self.cfg["click_through"]))
        through.setToolTip("Let clicks pass to whatever is underneath")
        through.triggered.connect(self._set_click_through)

        pop = menu.addAction("Pop to front on alert")
        pop.setCheckable(True)
        pop.setChecked(bool(self.cfg["pop_on_alert"]))
        pop.setToolTip("Raise and flash the widget when a session finishes or needs you")
        pop.triggered.connect(self._set_pop_on_alert)

        stats_line = menu.addAction("Show stats line")
        stats_line.setCheckable(True)
        stats_line.setChecked(bool(self.cfg.get("show_stats", True)))
        stats_line.setToolTip("Show today's tasks, tokens, cost and reply time on the tile")
        stats_line.triggered.connect(self._set_show_stats)

        isle = menu.addAction("Dynamic Island")
        isle.setCheckable(True)
        isle.setChecked(bool(self.cfg.get("island")))
        isle.setToolTip(
            "Compact pill of live Claude sessions and now-playing media"
            + ("" if media.MediaWatcher.available else " (media unavailable)")
        )
        isle.triggered.connect(self._set_island)

        if self.cfg.get("island"):
            isle_lock = menu.addAction("   Lock island to top")
            isle_lock.setCheckable(True)
            isle_lock.setChecked(bool(self.cfg.get("island_locked", True)))
            isle_lock.setToolTip(
                "Pinned top-centre on every screen, or free to drag on one"
            )
            isle_lock.triggered.connect(self._set_island_locked)

        water = menu.addAction("Water reminder")
        water.setCheckable(True)
        water.setChecked(bool(self.cfg.get("hydration_enabled", True)))
        water.setToolTip(
            f"Block every screen for "
            f"{int(self.cfg.get('hydration_block_seconds', 15))}s every "
            f"{int(self.cfg.get('hydration_minutes', 75))} min to drink water"
        )
        water.triggered.connect(self._set_hydration)
        water_now = menu.addAction("   Take a water break now")
        water_now.setToolTip("Preview the break overlay")
        water_now.triggered.connect(self._hydration_due)

        hidden = menu.addAction("Hide from screenshots")
        hidden.setCheckable(True)
        hidden.setChecked(bool(self.cfg["hide_from_capture"]))
        hidden.triggered.connect(self._set_hide_from_capture)

        lock = menu.addAction("Lock position")
        lock.setCheckable(True)
        lock.setChecked(bool(self.cfg["locked"]))
        lock.triggered.connect(self._toggle_lock)

        startup = menu.addAction("Start with Windows")
        startup.setCheckable(True)
        startup.setChecked(startup_enabled())
        startup.triggered.connect(set_startup)

        menu.addSeparator()
        team = menu.addMenu(
            f"Team: {identity_module.team_config().get('team_id') or 'not enrolled'}"
        )
        serve = team.addAction("Serve web dashboard")
        serve.setCheckable(True)
        serve.setChecked(bool(self.cfg.get("web_enabled")))
        serve.setToolTip(
            f"Listen on {self.cfg.get('web_bind', '127.0.0.1')}:"
            f"{self.cfg.get('web_port', 8765)}"
        )
        serve.triggered.connect(self._set_web_enabled)

        reporting = team.addAction("Report to the team project")
        reporting.setCheckable(True)
        reporting.setChecked(bool(self.cfg.get("sync_enabled", True)))
        reporting.setEnabled(identity_module.enrolled())
        reporting.setToolTip(
            "Upload finished tasks and token counts on a timer"
            if identity_module.enrolled()
            else "Enrol this laptop on the dashboard's Deploy tab first"
        )
        reporting.triggered.connect(self._set_sync_enabled)

        team.addSeparator()
        enrol = team.addAction("Set up a team…")
        enrol.setToolTip("Opens the dashboard's Deploy tab")
        enrol.triggered.connect(self._open_deploy_tab)

        menu.addSeparator()
        menu.addAction("Quit").triggered.connect(self.close)
        return menu

    def _radio_menu(self, menu: QMenu, options, current, handler) -> None:
        group = QActionGroup(menu)
        group.setExclusive(True)
        for label, value in options:
            action = QAction(label, menu)
            action.setCheckable(True)
            action.setChecked(value == current)
            action.triggered.connect(lambda _checked=False, v=value: handler(v))
            group.addAction(action)
            menu.addAction(action)

    def _set_interval(self, value: int) -> None:
        self.cfg["interval_ms"] = value
        save_config(self.cfg)

    def _set_opacity(self, value: float) -> None:
        self.cfg["opacity"] = value
        self.setWindowOpacity(value)
        save_config(self.cfg)

    def free_memory(self) -> None:
        """Trim working sets to hand physical RAM back. Terminates nothing."""
        if self._trimmer is not None and self._trimmer.isRunning():
            return
        self._show_flash("working...", hold=False)
        self._trimmer = MemoryTrimmer()
        self._trimmer.done.connect(self._on_trim_done)
        self._trimmer.start()

    def _on_trim_done(self, result) -> None:
        # Mark an unattended trim, so a number appearing by itself is explained.
        prefix = "auto " if self._auto_trim_pending else ""
        self._auto_trim_pending = False
        self._show_flash(f"{prefix}{result.summary}" if result else "trim failed")

    def _show_flash(self, text: str, hold: bool = True) -> None:
        """Briefly show a message where the header status sits."""
        self._flash = text
        if hold:
            QTimer.singleShot(5000, self._clear_flash)
        self.update()

    def _clear_flash(self) -> None:
        self._flash = ""
        self.update()

    def _set_bg_alpha(self, value: int) -> None:
        self.cfg["bg_alpha"] = value
        save_config(self.cfg)
        self.update()

    def _set_auto_trim(self, checked: bool) -> None:
        self.cfg["auto_trim"] = checked
        save_config(self.cfg)

    def _set_auto_trim_pct(self, value: int) -> None:
        self.cfg["auto_trim_pct"] = value
        # Let a new, lower threshold act at once rather than waiting out the
        # cooldown from a trim made under the old one.
        self._last_auto_trim = 0.0
        save_config(self.cfg)

    def _set_pop_on_alert(self, checked: bool) -> None:
        self.cfg["pop_on_alert"] = checked
        save_config(self.cfg)

    def _set_click_through(self, checked: bool) -> None:
        self.cfg["click_through"] = checked
        # Apply immediately and keep _is_transparent in sync; the hover timer
        # takes over from here.
        self._is_transparent = not checked  # force _set_transparent to act
        self._set_transparent(checked)
        save_config(self.cfg)

    def _set_hide_from_capture(self, checked: bool) -> None:
        self.cfg["hide_from_capture"] = checked
        set_capture_exclusion(self, checked)
        save_config(self.cfg)

    def _set_fan_mode(self, value: str) -> None:
        self.cfg["fan_mode"] = value
        # RPM and duty share a sparkline but not a scale, so drop the history
        # and let the next sample re-prime it.
        self._primed = False
        save_config(self.cfg)

    def _toggle_lock(self, checked: bool) -> None:
        self.cfg["locked"] = checked
        save_config(self.cfg)

    # -- remote control ------------------------------------------------------

    def apply_setting(self, key: str, value) -> bool:
        """Change one tile setting by name, as the menu would.

        The tray menu is no longer the only way to configure the tile: the
        dashboard offers the same settings as live controls, and the web
        dashboard offers them again over HTTP. Rather than let each of those
        reach into ``cfg`` and hope to remember the side effects -- reapplying
        the capture-exclusion flag, re-priming the fan history, resizing the
        window -- they all come through here, which dispatches to the same
        setter the menu item uses. One place to add a setting; one place for it
        to go wrong.

        Returns False for an unknown key or a value that will not coerce, so a
        caller (the HTTP API in particular) can answer with a real error
        instead of silently doing nothing.
        """
        setters = {
            "interval_ms": (int, self._set_interval),
            "slow_every": (int, self._set_slow_every),
            "opacity": (float, self._set_opacity),
            "bg_alpha": (int, self._set_bg_alpha),
            "scale": (float, self._set_scale),
            "fan_mode": (str, self._set_fan_mode),
            "fan_max_rpm": (int, self._set_fan_max_rpm),
            "click_through": (bool, self._set_click_through),
            "hide_from_capture": (bool, self._set_hide_from_capture),
            "pop_on_alert": (bool, self._set_pop_on_alert),
            "show_stats": (bool, self._set_show_stats),
            "island": (bool, self._set_island),
            "island_locked": (bool, self._set_island_locked),
            "hydration_enabled": (bool, self._set_hydration),
            "locked": (bool, self._toggle_lock),
            "auto_trim": (bool, self._set_auto_trim),
            "auto_trim_pct": (int, self._set_auto_trim_pct),
            "auto_trim_gb": (float, self._set_auto_trim_gb),
        }
        entry = setters.get(key)
        if entry is None:
            return False
        kind, setter = entry
        try:
            if kind is bool:
                # A checkbox arriving as JSON is a real bool; one arriving as a
                # query string is "true"/"1". Accept both.
                if isinstance(value, str):
                    value = value.strip().lower() in ("1", "true", "yes", "on")
                else:
                    value = bool(value)
            elif kind is str:
                value = str(value)
            else:
                value = kind(value)
        except (TypeError, ValueError):
            return False

        if key == "fan_mode" and value not in ("rpm", "duty"):
            return False
        if key == "opacity":
            value = max(0.2, min(1.0, value))
        if key == "bg_alpha":
            value = max(0, min(255, value))
        if key == "auto_trim_pct":
            value = max(50, min(99, value))

        setter(value)
        return True

    def _set_slow_every(self, value: int) -> None:
        self.cfg["slow_every"] = max(1, int(value))
        save_config(self.cfg)

    def _set_fan_max_rpm(self, value: int) -> None:
        self.cfg["fan_max_rpm"] = max(1000, int(value))
        save_config(self.cfg)
        self.update()

    def _set_auto_trim_gb(self, value: float) -> None:
        self.cfg["auto_trim_gb"] = max(1.0, float(value))
        self._last_auto_trim = 0.0
        save_config(self.cfg)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.cfg["x"], self.cfg["y"] = self.x(), self.y()
        save_config(self.cfg)
        if self._scorecard is not None:
            self._scorecard.close()
        if self._dashboard is not None:
            self._dashboard.close()
        if self._web is not None:
            self._web.stop()
            self._web = None
        self._stop_media()
        self._teardown_island_clones()
        if self._blocker is not None:
            self._blocker.close()
            self._blocker = None
        if self._syncer is not None:
            # One last push, so the final turns of a session are not stranded
            # on disk until the next launch.
            self._syncer.stop()
            self._syncer.wait(2500)
            self._syncer = None
            try:
                import sync as sync_module

                if identity_module.enrolled():
                    sync_module.push(sample=self.sample)
            except Exception:  # noqa: BLE001 - do not block the quit
                pass
        self.tray.hide()
        self.sampler.stop()
        self.sampler.wait(2500)
        event.accept()
        QApplication.quit()


class IslandClone(QWidget):
    """A mirror of the tile's island on a second monitor.

    It stores no state: it paints the owning tile's island and forwards clicks
    to it, so both screens show the same sessions and media and either is
    clickable. A slow timer keeps it in step (new alert, media, drop animation).
    """

    def __init__(self, tile: "MonitorWidget", screen) -> None:
        super().__init__()
        self.tile = tile
        self._screen = screen
        self._last_size: tuple | None = None
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(
            lambda pos: self.tile._build_menu().exec(self.mapToGlobal(pos))
        )
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sync)
        self._timer.start(16)   # 60fps: mirror the spinner/drop smoothly
        self.sync()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        set_capture_exclusion(self, bool(self.tile.cfg["hide_from_capture"]))
        set_click_through(self, bool(self.tile.cfg["click_through"]))

    def sync(self) -> None:
        """Match size/position to the tile's island and repaint."""
        scale = self.tile.scale or 1.0
        w, h = self.tile._island_logical_size()
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        if size != self._last_size:
            self.setFixedSize(*size)
            area = self._screen.geometry()
            self.move(area.center().x() - self.width() // 2, area.top())
            self._last_size = size
        # Pass clicks through wherever the cursor is off the visible card.
        over = self.tile._island_card_rect_global(self).contains(QCursor.pos())
        set_click_through(self, not over)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.scale(self.tile.scale, self.tile.scale)
        self.tile._paint_island(p)
        p.end()

    def _to_logical(self, point: QPoint) -> QPoint:
        s = self.tile.scale or 1.0
        return QPoint(round(point.x() / s), round(point.y() / s))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.tile._island_click(
            self._to_logical(event.position().toPoint())
        ):
            event.accept()


class DodgeButton(QPushButton):
    """A button that will not be clicked: it hops away from the cursor, so the
    only way past the water prompt is to say yes."""

    def __init__(self, text: str, bounds, parent=None) -> None:
        super().__init__(text, parent)
        self._bounds = bounds   # callable -> QRect it may roam within

    def _hop(self) -> None:
        r = self._bounds()
        x = random.randint(r.left(), max(r.left(), r.right() - self.width()))
        y = random.randint(r.top(), max(r.top(), r.bottom() - self.height()))
        self.move(x, y)

    def enterEvent(self, event) -> None:  # noqa: N802
        self._hop()
        super().enterEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._hop()           # even a fast click lands on empty space
        event.ignore()


class HydrationBlocker(QWidget):
    """A full-screen water break. It covers every screen and swallows input for
    a few seconds (no escape), then asks whether they drank: the No button runs
    away, and an instant Yes is double-checked, before the screen is released.
    """

    finished = Signal()

    def __init__(self, block_seconds: int = 15) -> None:
        super().__init__()
        self._left = int(block_seconds)
        self._phase = "block"            # block -> ask -> confirm
        self._ask_at = 0.0
        self._glass = False
        self.yes_btn: QPushButton | None = None
        self.no_btn: DodgeButton | None = None
        self.setWindowFlags(
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setCursor(Qt.ArrowCursor)
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)

    def begin(self) -> None:
        # Span the whole virtual desktop so no screen is usable.
        self.setGeometry(QApplication.primaryScreen().virtualGeometry())
        self.show()
        self.raise_()
        self.activateWindow()
        self.grabKeyboard()               # keystrokes cannot reach apps behind
        self._tick_timer.start(1000)
        self.update()

    def _central_bounds(self) -> QRect:
        """Where the buttons (and the dodging No) may sit -- centre of the
        primary screen, in this window's coordinates."""
        area = QApplication.primaryScreen().geometry()
        g = self.geometry()
        cx = area.center().x() - g.x()
        cy = area.center().y() - g.y()
        return QRect(cx - 320, cy - 40, 640, 200)

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._tick_timer.stop()
            self._to_ask()
        self.update()

    def _to_ask(self) -> None:
        self._phase = "ask"
        self._ask_at = time.monotonic()
        style = (
            "QPushButton{background:rgba(255,255,255,0.14);color:#eaf2ff;"
            "border:1px solid rgba(255,255,255,0.35);border-radius:14px;"
            "padding:12px 34px;font-size:17px;font-weight:600;}"
            "QPushButton:hover{background:rgba(255,255,255,0.26);}"
        )
        self.yes_btn = QPushButton("Yes, I drank", self)
        self.yes_btn.setStyleSheet(style)
        self.yes_btn.clicked.connect(self._on_yes)
        self.no_btn = DodgeButton("No", self._central_bounds, self)
        self.no_btn.setStyleSheet(style)
        for b in (self.yes_btn, self.no_btn):
            b.setCursor(Qt.PointingHandCursor)
            b.adjustSize()
        c = self._central_bounds().center()
        self.yes_btn.move(c.x() - self.yes_btn.width() - 18, c.y())
        self.no_btn.move(c.x() + 18, c.y())
        self.yes_btn.show()
        self.no_btn.show()
        self.update()

    def _on_yes(self) -> None:
        if self._phase == "ask" and time.monotonic() - self._ask_at < 2.0:
            # Too quick to have actually drunk -- make them confirm.
            self._phase = "confirm"
            if self.no_btn:
                self.no_btn.hide()
            self.yes_btn.setText("Yes, I'm sure")
            self.yes_btn.adjustSize()
            c = self._central_bounds().center()
            self.yes_btn.move(c.x() - self.yes_btn.width() // 2, c.y())
            self.update()
            return
        self._resume()

    def _resume(self) -> None:
        try:
            self.releaseKeyboard()
        except Exception:  # noqa: BLE001
            pass
        self.finished.emit()
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - swallow all keys
        event.accept()

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.releaseKeyboard()
        except Exception:  # noqa: BLE001
            pass
        super().closeEvent(event)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # Liquid-glass water wash. With real acrylic behind it, a lighter tint
        # lets the frosted desktop show through; without it, nearly opaque so
        # the work behind still cannot be read.
        a = (150, 160, 170) if self._glass else (236, 240, 244)
        g = QLinearGradient(0, 0, 0, h)
        g.setColorAt(0.0, QColor(10, 26, 48, a[0]))
        g.setColorAt(0.55, QColor(12, 40, 64, a[1]))
        g.setColorAt(1.0, QColor(8, 22, 40, a[2]))
        p.fillRect(self.rect(), g)

        cx = self._central_bounds().center().x()
        top = QApplication.primaryScreen().geometry().top() - self.y()
        base = top + QApplication.primaryScreen().geometry().height() // 2

        self._draw_drop(p, cx, base - 120, 46)
        p.setPen(QColor(230, 242, 255))
        p.setFont(QFont("Segoe UI", 40, QFont.Weight.Bold))
        self._centre(p, cx, base - 20, "Time to drink water")

        if self._phase == "block":
            p.setFont(QFont("Segoe UI", 20))
            p.setPen(QColor(170, 200, 230))
            self._centre(p, cx, base + 10,
                         f"Step away for {self._left}s…")
        elif self._phase == "ask":
            p.setFont(QFont("Segoe UI", 22, QFont.Weight.DemiBold))
            p.setPen(QColor(220, 235, 255))
            self._centre(p, cx, self._central_bounds().top() - 24,
                         "Did you drink water?")
        else:  # confirm
            p.setFont(QFont("Segoe UI", 22, QFont.Weight.DemiBold))
            p.setPen(QColor(220, 235, 255))
            self._centre(p, cx, self._central_bounds().top() - 24,
                         "Are you sure?")
        p.end()

    @staticmethod
    def _centre(p: QPainter, cx: int, baseline: int, text: str) -> None:
        wdt = QFontMetrics(p.font()).horizontalAdvance(text)
        p.drawText(int(cx - wdt / 2), int(baseline), text)

    @staticmethod
    def _draw_drop(p: QPainter, cx: float, cy: float, s: float) -> None:
        """A glassy water droplet, drawn rather than relying on an emoji glyph."""
        drop = QPainterPath()
        drop.moveTo(cx, cy - s)
        drop.cubicTo(cx + s * 0.95, cy - s * 0.1, cx + s * 0.75, cy + s * 0.75,
                     cx, cy + s)
        drop.cubicTo(cx - s * 0.75, cy + s * 0.75, cx - s * 0.95, cy - s * 0.1,
                     cx, cy - s)
        drop.closeSubpath()
        grad = QLinearGradient(cx, cy - s, cx, cy + s)
        grad.setColorAt(0.0, QColor(120, 200, 255))
        grad.setColorAt(1.0, QColor(40, 120, 210))
        p.setPen(Qt.NoPen)
        p.fillPath(drop, grad)
        p.setBrush(QColor(255, 255, 255, 150))      # specular highlight
        p.drawEllipse(QRectF(cx - s * 0.35, cy - s * 0.1, s * 0.3, s * 0.45))


class ScorecardWindow(QWidget):
    """A normal, readable window of agent-productivity metrics.

    Unlike the tile this is not frameless or click-through -- it is meant to be
    opened, read and closed. It mirrors the tile's screenshot setting, so a
    board of token spend and cost stays out of shared screens when the tile
    does. It refreshes itself every few seconds while visible.
    """

    PERIODS = (("Today", 0), ("7 days", 7), ("30 days", 30))

    def __init__(self, cfg: dict) -> None:
        super().__init__()
        self.cfg = cfg
        self._period = 7
        self.setWindowTitle("Agent Scorecard")
        self.resize(560, 620)
        self.setStyleSheet(
            "QWidget { background: #0e1119; }"
            "QPushButton { background: #1a1e2a; color: #cdd3df;"
            " border: 1px solid #2a2f3a; border-radius: 6px; padding: 5px 14px; }"
            "QPushButton:hover { background: #222838; }"
            "QPushButton:checked { background: #2b3242; color: #ffffff;"
            " border-color: #3a4152; }"
            "QTextBrowser { background: #0e1119; border: none; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)

        bar = QHBoxLayout()
        self._buttons: dict[int, QPushButton] = {}
        for label, days in self.PERIODS:
            button = QPushButton(label)
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, d=days: self._set_period(d))
            bar.addWidget(button)
            self._buttons[days] = button
        bar.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        layout.addLayout(bar)

        self.view = QTextBrowser()
        self.view.setOpenExternalLinks(False)
        layout.addWidget(self.view)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(5000)
        self._sync_buttons()

    def _auto_refresh(self) -> None:
        if self.isVisible():
            self.refresh()

    def _set_period(self, days: int) -> None:
        self._period = days
        self._sync_buttons()
        self.refresh()

    def _sync_buttons(self) -> None:
        for days, button in self._buttons.items():
            button.setChecked(days == self._period)

    def refresh(self) -> None:
        try:
            html = stats.report_html(self._period)
        except Exception as exc:  # noqa: BLE001 - show the error, never crash
            html = f"<body style='color:#f87171;font-family:sans-serif'>stats error: {exc}</body>"
        self.view.setHtml(html)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        try:
            set_capture_exclusion(self, bool(self.cfg.get("hide_from_capture", True)))
        except Exception:  # noqa: BLE001
            pass


def _tray_icon() -> QIcon:
    """A small bar-chart glyph, drawn rather than shipped as a file."""
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.transparent)
    p = QPainter(pixmap)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    for i, (name, height) in enumerate(
        (("CPU", 12), ("RAM", 20), ("GPU", 9), ("FAN", 16))
    ):
        p.setBrush(ACCENTS[name])
        p.drawRoundedRect(QRectF(3 + i * 7.5, 28 - height, 5, height), 1.5, 1.5)
    p.end()
    return QIcon(pixmap)


def bring_to_front(window) -> None:
    """Raise the widget above other windows without taking keyboard focus.

    SWP_NOACTIVATE is the point: the widget jumps to the top of the Z-order but
    does not steal focus, so whatever the user is typing into keeps it. The
    topmost flag is re-asserted at the same time, since a fullscreen app may
    have displaced it.
    """
    import ctypes

    HWND_TOPMOST = -1
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOACTIVATE = 0x0010
    SWP_SHOWWINDOW = 0x0040
    try:
        handle = int(window.winId())
    except (TypeError, ValueError):
        return
    ctypes.windll.user32.SetWindowPos(
        handle, HWND_TOPMOST, 0, 0, 0, 0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
    )


def bring_agent_to_front(project: str, self_hwnd: int = 0) -> bool:
    """Focus the terminal/editor window of the session working on ``project``.

    There is no direct handle from a session to its window, so this matches the
    project folder name against visible top-level window titles -- editors and
    terminals almost always put the open folder or cwd in the title bar.

    Matching ignores punctuation and case: the title bar may render the folder
    as "B2Consultants" while the project key is "B2-Consultants", so both sides
    are reduced to their letters and digits before comparing. A non-minimised
    match is preferred. The widget's own window is skipped.

    Returns True when a match was found and activated. A heuristic, so it can
    miss or, with a very generic project name, pick the wrong window.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    needle = _alnum(project)
    if not needle:
        return False

    matches: list[int] = []

    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _collect(hwnd, _lparam):
        if hwnd == self_hwnd or not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if needle in _alnum(buf.value):
            matches.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(_collect), 0)
    if not matches:
        return False

    # Raising a minimised window would restore it; prefer one already open.
    hwnd = next((h for h in matches if not user32.IsIconic(h)), matches[0])
    return focus_window(hwnd)


def focus_window(hwnd: int) -> bool:
    """Activate one specific window by handle. False if it is gone.

    Windows blocks a background process from calling SetForegroundWindow
    directly; attaching to the target window's input thread first is the
    standard way around it.
    """
    import ctypes

    user32 = ctypes.windll.user32
    if not hwnd or not user32.IsWindow(hwnd) or not user32.IsWindowVisible(hwnd):
        return False

    target_tid = user32.GetWindowThreadProcessId(hwnd, None)
    our_tid = ctypes.windll.kernel32.GetCurrentThreadId()
    SW_RESTORE = 9
    user32.AttachThreadInput(our_tid, target_tid, True)
    try:
        # Only un-minimise -- an unconditional SW_RESTORE would also
        # un-maximise a maximised editor, which is not what a raise should do.
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        user32.AttachThreadInput(our_tid, target_tid, False)
    return True


def _alnum(text: str) -> str:
    """Lower-cased letters and digits only, for punctuation-insensitive match."""
    return "".join(ch for ch in (text or "").lower() if ch.isalnum())


def set_click_through(window, enabled: bool) -> bool:
    """Let mouse clicks fall through the widget to whatever is underneath.

    Adds WS_EX_TRANSPARENT to the window's extended style, which makes Windows
    skip it during hit-testing: clicks, scrolls and hovers all land on the
    window below. WS_EX_LAYERED has to be present for this to work, and Qt
    already sets it for a translucent window.

    This is done with SetWindowLong rather than Qt's WindowTransparentForInput
    flag because changing Qt window flags destroys and recreates the native
    window, which would drop the capture-exclusion affinity set alongside it.

    While enabled the widget cannot be dragged or right-clicked -- the tray
    icon carries the menu.
    """
    import ctypes

    GWL_EXSTYLE = -20
    WS_EX_TRANSPARENT = 0x00000020
    user32 = ctypes.windll.user32
    try:
        handle = int(window.winId())
    except (TypeError, ValueError):
        return False

    # SetWindowLongW is the 32-bit-safe name; SetWindowLongPtrW only exists on
    # 64-bit builds, and the EXSTYLE value fits in a LONG either way.
    style = user32.GetWindowLongW(handle, GWL_EXSTYLE)
    updated = style | WS_EX_TRANSPARENT if enabled else style & ~WS_EX_TRANSPARENT
    user32.SetWindowLongW(handle, GWL_EXSTYLE, updated)
    return user32.GetWindowLongW(handle, GWL_EXSTYLE) == updated


def set_capture_exclusion(window, hidden: bool) -> bool:
    """Hide the window from screenshots and screen shares, or stop hiding it.

    WDA_EXCLUDEFROMCAPTURE tells the compositor to leave this window out of
    anything that captures the screen -- Snipping Tool, Print Screen, OBS,
    Teams and Zoom shares -- while it stays fully visible on the physical
    display. Windows 10 2004 and newer; older builds would blank the window
    to black instead, so a failed call is reported rather than ignored.
    """
    import ctypes

    WDA_NONE = 0x00000000
    WDA_EXCLUDEFROMCAPTURE = 0x00000011
    affinity = WDA_EXCLUDEFROMCAPTURE if hidden else WDA_NONE
    try:
        handle = int(window.winId())
    except (TypeError, ValueError):
        return False
    return bool(ctypes.windll.user32.SetWindowDisplayAffinity(handle, affinity))


def _temp_colour(temp: float | None) -> QColor | None:
    if temp is None:
        return None
    if temp >= 90:
        return HOT
    if temp >= 80:
        return WARM
    return None


# -- run at logon ----------------------------------------------------------
#
# A scheduled task is the only way to auto-start an elevated process without a
# UAC prompt at each logon, so "Start with Windows" registers one with
# highest-privileges rather than using a Startup-folder shortcut.


def _schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks"] + args, capture_output=True, text=True, creationflags=NO_WINDOW
    )


def startup_enabled() -> bool:
    return _schtasks(["/Query", "/TN", TASK_NAME]).returncode == 0


def set_startup(enable: bool) -> bool:
    if not enable:
        return _schtasks(["/Delete", "/TN", TASK_NAME, "/F"]).returncode == 0

    # pythonw.exe runs without a console window.
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pythonw if os.path.exists(pythonw) else sys.executable
    command = f'"{runner}" "{os.path.join(HERE, "widget.py")}"'
    created = _schtasks(
        ["/Create", "/TN", TASK_NAME, "/TR", command,
         "/SC", "ONLOGON", "/RL", "HIGHEST", "/F"]
    )
    return created.returncode == 0


def already_running() -> bool:
    """True when another copy of the widget holds the named mutex.

    Without this, launching run.vbs twice -- or once elevated and once not --
    leaves two widgets stacked on the desktop, each writing config.json.
    The handle is intentionally leaked so Windows holds it for this process's
    lifetime and releases it automatically on exit.
    """
    import ctypes

    ERROR_ALREADY_EXISTS = 183
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "SystemMonitorWidget.singleton")
    return kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def request_summon() -> None:
    """Ask the already-running instance to make itself reachable again."""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        open(SUMMON_PATH, "w").close()
    except OSError:
        pass


def main() -> int:
    if already_running():
        # Don't start a second copy; instead signal the running one to surface.
        request_summon()
        return 0

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    # Held in a local: dropping the last reference would let Python collect the
    # widget, destroying the window and quitting the app immediately.
    widget = MonitorWidget()
    widget.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
