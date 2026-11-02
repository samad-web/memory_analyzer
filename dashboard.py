r"""The dashboard: every task, what it cost, and the controls for the tile.

The old Scorecard was a single page of rendered HTML in a text browser -- fine
for a glance, but read-only and fixed. This replaces it with five tabs:

    Overview   headline figures, the daily trend, where the tokens went
    Tasks      every task as a row, with the prompt it started from
    Team       one card per laptop: who is working, blocked, or throttling
    Tile       the tile's own settings, as live controls
    Deploy     enrollment, viewer sign-in, and the teammate installer

Two things are worth knowing about the shape of it.

**Local and team data share one code path.** ``stats.task_rows`` turns the local
event log into task records; ``sync`` turns the cloud's rows into records of the
same shape. Everything above that -- every statistic, chart and table -- is
written once and runs over whichever list the scope selector hands it. The
team view is not a second implementation of the overview.

**The Tile tab drives the real tile, not a copy of its config.** Each control
calls ``MonitorWidget.apply_setting``, which is the same code path the tray menu
uses, so a change lands on screen as you make it and cannot drift from what the
menu would have done. Opened standalone, with no tile running, the tab writes
``config.json`` instead and says so rather than pretending to be live.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import charts
import cloud
import events
import identity
import stats
import sync
import viz

HERE = os.path.dirname(os.path.abspath(__file__))

PERIODS = (("Today", 0), ("7 days", 7), ("30 days", 30))

# The dashboard repaints on this cadence while visible. Slower than the tile:
# the numbers it shows move in turns, not in frames, and re-aggregating the log
# is real work.
REFRESH_MS = 6000

STYLE = f"""
QWidget {{ background: {viz.SURFACE}; color: {viz.INK};
           font-family: 'Segoe UI'; font-size: 12px; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ background: transparent; color: {viz.INK_DIM};
                padding: 7px 16px; margin-right: 2px;
                border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {viz.INK}; border-bottom: 2px solid {viz.SERIES[0]}; }}
QTabBar::tab:hover {{ color: {viz.INK}; }}
QPushButton {{ background: {viz.SURFACE_RAISED}; color: {viz.INK_DIM};
               border: 1px solid {viz.BORDER}; border-radius: 6px;
               padding: 5px 14px; }}
QPushButton:hover {{ background: #222838; color: {viz.INK}; }}
QPushButton:checked {{ background: #2b3242; color: #ffffff; border-color: #3a4152; }}
QPushButton:disabled {{ color: {viz.INK_MUTED}; border-color: {viz.BORDER_SOFT}; }}
QGroupBox {{ border: 1px solid {viz.BORDER_SOFT}; border-radius: 8px;
             margin-top: 14px; padding: 12px 12px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px;
                    color: {viz.INK_DIM}; font-weight: 600; }}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit {{
    background: {viz.SURFACE_RAISED}; border: 1px solid {viz.BORDER};
    border-radius: 5px; padding: 4px 7px; color: {viz.INK};
    selection-background-color: {viz.SERIES[0]}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {viz.SERIES[0]}; }}
QCheckBox {{ color: {viz.INK_DIM}; spacing: 7px; }}
QCheckBox::indicator {{ width: 14px; height: 14px; border-radius: 4px;
    border: 1px solid {viz.BORDER}; background: {viz.SURFACE_RAISED}; }}
QCheckBox::indicator:checked {{ background: {viz.SERIES[0]};
    border-color: {viz.SERIES[0]}; }}
QTableWidget {{ background: {viz.SURFACE}; border: 1px solid {viz.BORDER_SOFT};
    border-radius: 8px; gridline-color: {viz.BORDER_SOFT};
    selection-background-color: #222838; selection-color: {viz.INK}; }}
QHeaderView::section {{ background: {viz.SURFACE_CARD}; color: {viz.INK_MUTED};
    border: none; border-bottom: 1px solid {viz.BORDER}; padding: 6px 8px;
    font-weight: 600; }}
QTableWidget::item {{ padding: 4px 8px; }}
QScrollArea {{ border: none; }}
QScrollBar:vertical {{ background: transparent; width: 9px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2a2f3a; border-radius: 4px;
    min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: #3a4152; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QSlider::groove:horizontal {{ height: 4px; background: {viz.GRID};
    border-radius: 2px; }}
QSlider::sub-page:horizontal {{ background: {viz.SERIES[0]}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {viz.INK}; width: 12px; height: 12px;
    margin: -5px 0; border-radius: 6px; }}
"""


def _label(text: str, colour: str = viz.INK_DIM, size: int = 11, bold: bool = False):
    item = QLabel(text)
    weight = "600" if bold else "400"
    item.setStyleSheet(
        f"color: {colour}; font-size: {size}px; font-weight: {weight};"
        " background: transparent;"
    )
    return item


def _heading(text: str):
    return _label(text.upper(), viz.INK_MUTED, 10, bold=True)


def _card() -> tuple[QFrame, QVBoxLayout]:
    frame = QFrame()
    frame.setStyleSheet(
        f"QFrame {{ background: {viz.SURFACE_CARD};"
        f" border: 1px solid {viz.BORDER_SOFT}; border-radius: 10px; }}"
    )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 12, 14, 12)
    layout.setSpacing(8)
    return frame, layout


class StatTile(QFrame):
    """A hero number with its caption. Not a chart -- a single value's job is
    to be read, and a one-bar chart of it would be decoration."""

    def __init__(self, caption: str, accent: str = viz.INK, hint: str = "") -> None:
        super().__init__()
        self.setStyleSheet(
            f"QFrame {{ background: {viz.SURFACE_CARD};"
            f" border: 1px solid {viz.BORDER_SOFT}; border-radius: 10px; }}"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(13, 10, 13, 10)
        layout.setSpacing(1)
        self.value = QLabel("--")
        self.value.setStyleSheet(
            f"color: {accent}; font-family: Consolas, monospace;"
            " font-size: 21px; font-weight: 600; background: transparent;"
        )
        self.caption = _label(caption, viz.INK_MUTED, 10)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)
        if hint:
            self.setToolTip(hint)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set(self, text: str) -> None:
        self.value.setText(text)


class _Worker(QObject):
    """Runs a cloud round-trip off the UI thread.

    Pulling the team board is an HTTP call that can sit on a dead network for
    the full timeout. Doing that on the UI thread would freeze the dashboard
    mid-repaint, which is exactly the sort of thing that makes a monitoring
    tool feel worse than the thing it monitors.
    """

    finished = Signal(dict)

    def __init__(self, days: int) -> None:
        super().__init__()
        self.days = days

    def run(self) -> None:
        try:
            report = sync.once(days=self.days)
        except Exception as exc:  # noqa: BLE001
            report = {"ok": False, "error": str(exc)[:200]}
        self.finished.emit(report)


class DashboardWindow(QWidget):
    """The dashboard window. ``tile`` is the running widget, when there is one."""

    def __init__(self, cfg: dict, tile=None) -> None:
        super().__init__()
        self.cfg = cfg
        self.tile = tile
        self.period = 7
        self.scope = "me"
        self._thread: QThread | None = None
        self._worker: _Worker | None = None
        self._rows: list[dict] = []
        self._events_cache: list[dict] | None = None

        self.setWindowTitle("Agent Dashboard")
        self.resize(1080, 760)
        self.setStyleSheet(STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 14, 16, 12)
        outer.setSpacing(10)
        outer.addLayout(self._build_toolbar())

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_overview(), "Overview")
        self.tabs.addTab(self._build_tasks(), "Tasks")
        self.tabs.addTab(self._build_team(), "Team")
        self.tabs.addTab(self._build_tile(), "Tile")
        self.tabs.addTab(self._build_deploy(), "Deploy")
        outer.addWidget(self.tabs, 1)

        self.footer = _label("", viz.INK_MUTED, 10)
        outer.addWidget(self.footer)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._auto_refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    # -- toolbar -----------------------------------------------------------

    def _build_toolbar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setSpacing(6)

        self._period_buttons: dict[int, QPushButton] = {}
        for text, days in PERIODS:
            button = QPushButton(text)
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, d=days: self._set_period(d))
            bar.addWidget(button)
            self._period_buttons[days] = button

        bar.addSpacing(14)
        self._scope_buttons: dict[str, QPushButton] = {}
        for text, scope in (("This laptop", "me"), ("Team", "team")):
            button = QPushButton(text)
            button.setCheckable(True)
            button.clicked.connect(lambda _c=False, s=scope: self._set_scope(s))
            bar.addWidget(button)
            self._scope_buttons[scope] = button

        bar.addStretch(1)
        self.sync_note = _label("", viz.INK_MUTED, 10)
        bar.addWidget(self.sync_note)
        self.sync_button = QPushButton("Sync now")
        self.sync_button.clicked.connect(self._sync_now)
        bar.addWidget(self.sync_button)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        bar.addWidget(refresh)
        self._sync_buttons()
        return bar

    def _set_period(self, days: int) -> None:
        self.period = days
        self._sync_buttons()
        self.refresh()

    def _set_scope(self, scope: str) -> None:
        self.scope = scope
        self._sync_buttons()
        self.refresh()

    def _sync_buttons(self) -> None:
        for days, button in self._period_buttons.items():
            button.setChecked(days == self.period)
        for scope, button in self._scope_buttons.items():
            button.setChecked(scope == self.scope)

    # -- overview tab ------------------------------------------------------

    def _build_overview(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(12)

        self.tiles = {
            "tasks": StatTile("tasks finished", viz.STATUS["done"],
                              "Turns the agent completed -- one Stop hook each"),
            "tokens": StatTile("tokens used", viz.SERIES[0],
                               "Read from the session transcript, once per turn"),
            "cost": StatTile("estimated cost", viz.SERIES[1],
                             "Tokens priced with the table in stats.PRICING -- an estimate"),
            "task_time": StatTile("median task", viz.INK,
                                  "Prompt to finish, for turns that completed"),
            "reply": StatTile("your reply time", viz.HEALTH["cpu"],
                              "How long an agent sat blocked before you answered"),
            "tools": StatTile("tool-calls / task", viz.INK_DIM,
                              "A rough proxy for how much work a task took"),
        }
        row = QHBoxLayout()
        row.setSpacing(9)
        for key in ("tasks", "tokens", "cost", "task_time", "reply", "tools"):
            row.addWidget(self.tiles[key])
        layout.addLayout(row)

        trend_card, trend_layout = _card()
        self.trend_title = _heading("Tokens per day")
        trend_layout.addWidget(self.trend_title)
        self.trend = charts.ColumnChart(value_key="tokens", colour=viz.SERIES[0])
        trend_layout.addWidget(self.trend)
        layout.addWidget(trend_card)

        split = QHBoxLayout()
        split.setSpacing(12)

        model_card, model_layout = _card()
        model_layout.addWidget(_heading("Tokens by model"))
        self.donut = charts.Donut()
        model_layout.addWidget(self.donut)
        split.addWidget(model_card, 1)

        project_card, project_layout = _card()
        self.project_title = _heading("Tokens by project")
        project_layout.addWidget(self.project_title)
        self.project_bars = charts.RankedBars(value_key="tokens", limit=8)
        project_layout.addWidget(self.project_bars)
        project_layout.addStretch(1)
        split.addWidget(project_card, 1)
        layout.addLayout(split)

        hour_card, hour_layout = _card()
        hour_layout.addWidget(_heading("When the work happens (tokens by hour)"))
        self.hours = charts.HourStrip()
        hour_layout.addWidget(self.hours)
        layout.addWidget(hour_card)

        layout.addStretch(1)
        page.setWidget(body)
        return page

    # -- tasks tab ---------------------------------------------------------

    COLUMNS = (
        ("Finished", 108),
        ("Member", 96),
        ("Project", 130),
        ("Task", 330),
        ("Took", 62),
        ("Reply", 62),
        ("Tokens", 74),
        ("Cost", 70),
        ("Tools", 52),
        ("Model", 92),
    )

    def _build_tasks(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 8, 2, 4)
        layout.setSpacing(8)

        bar = QHBoxLayout()
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText(
            "Filter by project, member, model or words in the task…"
        )
        self.filter_box.textChanged.connect(self._fill_tasks)
        bar.addWidget(self.filter_box, 1)
        self.task_count = _label("", viz.INK_MUTED, 10)
        bar.addWidget(self.task_count)
        export = QPushButton("Export CSV…")
        export.clicked.connect(self._export_csv)
        bar.addWidget(export)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels([c[0] for c in self.COLUMNS])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        for index, (_name, width) in enumerate(self.COLUMNS):
            self.table.setColumnWidth(index, width)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        return page

    def _fill_tasks(self) -> None:
        needle = self.filter_box.text().strip().lower()
        rows = self._rows
        if needle:
            rows = [
                r
                for r in rows
                if needle in str(r.get("project", "")).lower()
                or needle in str(r.get("prompt", "")).lower()
                or needle in str(r.get("member", "")).lower()
                or needle in str(r.get("model", "")).lower()
            ]

        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(rows))
        for index, task in enumerate(rows):
            when = time.strftime("%d %b %H:%M", time.localtime(task.get("ended_at", 0)))
            prompt = task.get("prompt") or ""
            if not prompt:
                # An honest placeholder beats an empty cell: these are turns
                # whose prompt was never recorded (a session that predates the
                # prompt logging, or a member who shares counts but not text).
                prompt = "— no prompt recorded —"
            cells = [
                (when, task.get("ended_at", 0), viz.INK_DIM),
                (task.get("member", "me"), None, viz.INK_DIM),
                (task.get("project", ""), None, viz.INK),
                (viz.clip(prompt, 160), None,
                 viz.INK if task.get("prompt") else viz.INK_MUTED),
                (viz.fmt_duration(task.get("duration_s")),
                 task.get("duration_s") or 0, viz.INK_DIM),
                (viz.fmt_duration(task.get("blocked_s") or None),
                 task.get("blocked_s") or 0, viz.HEALTH["cpu"]),
                (viz.fmt_tokens(task.get("tokens")), task.get("tokens") or 0,
                 viz.SERIES[0]),
                (viz.fmt_cost(task.get("cost")), task.get("cost") or 0,
                 viz.SERIES[1]),
                (str(task.get("tools") or 0), task.get("tools") or 0, viz.INK_DIM),
                (stats.model_family(task.get("model", "")), None,
                 viz.model_color(stats.model_family(task.get("model", "")))),
            ]
            for column, (text, sort_value, colour) in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if sort_value is not None:
                    # Sort on the number, not on "1.3M" -- a string sort would
                    # put 900K above 1.3M.
                    item.setData(Qt.UserRole, sort_value)
                    item.setData(Qt.EditRole, float(sort_value))
                item.setForeground(QColor(colour))
                if column in (4, 5, 6, 7, 8):
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if column == 3 and task.get("prompt"):
                    item.setToolTip(task["prompt"])
                self.table.setItem(index, column, item)
        self.table.setSortingEnabled(True)

        total = len(self._rows)
        shown = len(rows)
        self.task_count.setText(
            f"{shown} of {total} tasks" if shown != total else f"{total} tasks"
        )
        # The member column is noise on a single-laptop view.
        self.table.setColumnHidden(1, self.scope != "team")

    def _export_csv(self) -> None:
        import csv

        from PySide6.QtWidgets import QFileDialog

        default = os.path.join(
            os.path.expanduser("~"),
            f"agent-tasks-{time.strftime('%Y%m%d')}.csv",
        )
        path, _filter = QFileDialog.getSaveFileName(
            self, "Export tasks", default, "CSV files (*.csv)"
        )
        if not path:
            return
        fields = [
            "ended_at", "member", "project", "prompt", "duration_s", "blocked_s",
            "waits", "tokens", "tok_in", "tok_out", "tok_cache_create",
            "tok_cache_read", "cost", "tools", "model", "sid",
        ]
        try:
            with open(path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(fields)
                for task in self._rows:
                    tok = task.get("tok") or {}
                    writer.writerow(
                        [
                            time.strftime(
                                "%Y-%m-%d %H:%M:%S",
                                time.localtime(task.get("ended_at", 0)),
                            ),
                            task.get("member", "me"),
                            task.get("project", ""),
                            task.get("prompt", ""),
                            round(task.get("duration_s") or 0, 1),
                            round(task.get("blocked_s") or 0, 1),
                            task.get("waits") or 0,
                            task.get("tokens") or 0,
                            tok.get("in", 0),
                            tok.get("out", 0),
                            tok.get("cc", 0),
                            tok.get("cr", 0),
                            round(task.get("cost") or 0, 4),
                            task.get("tools") or 0,
                            task.get("model", ""),
                            task.get("sid", ""),
                        ]
                    )
        except OSError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return
        self._flash_footer(f"Exported {len(self._rows)} tasks to {path}")

    # -- team tab ----------------------------------------------------------

    def _build_team(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(12)

        self.team_banner = _label("", viz.INK_DIM, 11)
        self.team_banner.setWordWrap(True)
        layout.addWidget(self.team_banner)

        row = QHBoxLayout()
        row.setSpacing(9)
        self.team_tiles = {
            "members": StatTile("laptops reporting", viz.INK),
            "online": StatTile("online now", viz.STATUS["done"]),
            "working": StatTile("working now", viz.STATUS["working"]),
            "waiting": StatTile("blocked on a person", viz.STATUS["needs_input"]),
            "tokens": StatTile("team tokens", viz.SERIES[0]),
            "cost": StatTile("team cost", viz.SERIES[1]),
        }
        for key in ("members", "online", "working", "waiting", "tokens", "cost"):
            row.addWidget(self.team_tiles[key])
        layout.addLayout(row)

        member_card, member_layout = _card()
        member_layout.addWidget(_heading("Tokens by member"))
        self.member_bars = charts.RankedBars(value_key="tokens", limit=8)
        member_layout.addWidget(self.member_bars)
        layout.addWidget(member_card)

        fleet_card, fleet_layout = _card()
        fleet_layout.addWidget(_heading("Laptops"))
        self.fleet_host = QWidget()
        self.fleet_layout = QVBoxLayout(self.fleet_host)
        self.fleet_layout.setContentsMargins(0, 0, 0, 0)
        self.fleet_layout.setSpacing(8)
        fleet_layout.addWidget(self.fleet_host)
        layout.addWidget(fleet_card)

        layout.addStretch(1)
        page.setWidget(body)
        return page

    def _fill_team(self) -> None:
        state = sync.status()
        cache = sync.read_cache()
        backend = cloud.Backend.from_team_config()

        if not state["enrolled"]:
            self.team_banner.setText(
                "This laptop is not enrolled in a team. Set it up on the "
                "Deploy tab — nothing is sent anywhere until you do."
            )
        elif not backend.signed_in:
            self.team_banner.setText(
                "Enrolled and reporting. Sign in as a viewer on the Deploy tab "
                "to read the team board back — the key every laptop carries "
                "can write but not read."
            )
        else:
            fetched = cache.get("fetched_at") or 0
            note = f"Team board fetched {viz.fmt_ago(time.time() - fetched)}."
            if cache.get("error"):
                note += f"  Last error: {cache['error']}"
            self.team_banner.setText(note)

        devices = sync.team_devices(cache)
        sessions = sync.live_sessions(cache)
        self.team_tiles["members"].set(str(len(devices)))
        self.team_tiles["online"].set(str(sum(1 for d in devices if d["online"])))
        self.team_tiles["working"].set(
            str(sum(1 for s in sessions if s.get("state") == "working"))
        )
        self.team_tiles["waiting"].set(
            str(sum(1 for s in sessions if s.get("state") == "needs_input"))
        )

        team_rows = self._team_rows()
        summary = stats.summarize_tasks(team_rows)
        self.team_tiles["tokens"].set(viz.fmt_tokens(summary["tokens"]))
        self.team_tiles["cost"].set(viz.fmt_cost(summary["cost"]))
        self.member_bars.set_rows(summary["member_rows"])

        while self.fleet_layout.count():
            item = self.fleet_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not devices:
            self.fleet_layout.addWidget(
                _label(
                    "No laptops reporting yet. The Deploy tab builds the "
                    "installer that enrolls one.",
                    viz.INK_MUTED,
                )
            )
            return
        for device in devices:
            self.fleet_layout.addWidget(self._device_card(device, team_rows))

    def _device_card(self, device: dict, team_rows: list[dict]) -> QWidget:
        frame = QFrame()
        frame.setStyleSheet(
            f"QFrame {{ background: {viz.SURFACE}; border: 1px solid {viz.BORDER_SOFT};"
            " border-radius: 8px; }"
        )
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        head = QHBoxLayout()
        dot = QLabel("●")
        state = "done" if device["online"] else "offline"
        dot.setStyleSheet(
            f"color: {viz.STATUS[state]}; font-size: 13px; background: transparent;"
        )
        head.addWidget(dot)
        head.addWidget(_label(device["member"], viz.INK, 13, bold=True))
        head.addWidget(_label(f"· {device['host']}", viz.INK_MUTED, 10))
        head.addStretch(1)
        # The status word is spelled out beside the dot; the colour is a second
        # encoding, never the only one.
        head.addWidget(
            _label(
                "online" if device["online"]
                else f"last seen {viz.fmt_ago(device['offline_s'])}",
                viz.STATUS[state],
                10,
            )
        )
        layout.addLayout(head)

        mine = [r for r in team_rows if r.get("device_id") == device["device_id"]]
        totals = stats.summarize_tasks(mine)
        facts = QHBoxLayout()
        facts.setSpacing(18)
        for text, value in (
            ("tasks", str(totals["tasks"])),
            ("tokens", viz.fmt_tokens(totals["tokens"])),
            ("cost", viz.fmt_cost(totals["cost"])),
            ("median task", viz.fmt_duration(totals["task_median"])),
            ("reply", viz.fmt_duration(totals["reply_mean"])),
        ):
            cell = QVBoxLayout()
            cell.setSpacing(0)
            cell.addWidget(_label(value, viz.INK, 13, bold=True))
            cell.addWidget(_label(text, viz.INK_MUTED, 9))
            facts.addLayout(cell)
        facts.addStretch(1)

        health = QVBoxLayout()
        health.setSpacing(2)
        if device.get("cpu_pct") is not None:
            cpu = charts.Meter("CPU", viz.HEALTH["cpu"])
            temp = device.get("cpu_temp")
            cpu.set_value(
                (device["cpu_pct"] or 0) / 100.0,
                f"{device['cpu_pct']:.0f}%"
                + (f"  {temp:.0f}°C" if temp else ""),
            )
            cpu.setFixedWidth(230)
            health.addWidget(cpu)
        if device.get("ram_pct") is not None:
            ram = charts.Meter("RAM", viz.HEALTH["ram"])
            used, total = device.get("ram_used_gb"), device.get("ram_total_gb")
            ram.set_value(
                (device["ram_pct"] or 0) / 100.0,
                f"{device['ram_pct']:.0f}%"
                + (f"  {used:.1f}/{total:.0f}G" if used and total else ""),
            )
            ram.setFixedWidth(230)
            health.addWidget(ram)
        if device.get("gpu_pct") is not None:
            gpu = charts.Meter("GPU", viz.HEALTH["gpu"])
            temp = device.get("gpu_temp")
            gpu.set_value(
                (device["gpu_pct"] or 0) / 100.0,
                f"{device['gpu_pct']:.0f}%"
                + (f"  {temp:.0f}°C" if temp else ""),
            )
            gpu.setFixedWidth(230)
            health.addWidget(gpu)
        if health.count():
            facts.addLayout(health)
        layout.addLayout(facts)

        for session in device.get("sessions", []):
            line = QHBoxLayout()
            line.setSpacing(7)
            state = str(session.get("state") or "working")
            pip = QLabel("●")
            pip.setStyleSheet(
                f"color: {viz.status_color(state)}; font-size: 10px;"
                " background: transparent;"
            )
            line.addWidget(pip)
            line.addWidget(
                _label(viz.STATUS_LABEL.get(state, state), viz.status_color(state), 10)
            )
            line.addWidget(_label(str(session.get("project") or ""), viz.INK_DIM, 10))
            message = viz.clip(session.get("message") or "", 70)
            line.addWidget(_label(message, viz.INK_MUTED, 10), 1)
            line.addWidget(_label(viz.fmt_duration(session.get("age_s")), viz.INK_MUTED, 10))
            layout.addLayout(line)

        return frame

    # -- tile tab ----------------------------------------------------------

    def _build_tile(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(4)

        live = self.tile is not None
        layout.addWidget(
            _label(
                "Every control here drives the running tile as you change it."
                if live
                else "No tile is running, so these are written to config.json "
                     "and take effect the next time it starts.",
                viz.INK_DIM if live else viz.STATUS["needs_input"],
                11,
            )
        )

        appearance = QGroupBox("Appearance")
        form = QFormLayout(appearance)
        form.setLabelAlignment(Qt.AlignRight)
        form.setSpacing(9)

        self.scale_slider = QSlider(Qt.Horizontal)
        self.scale_slider.setRange(60, 250)
        self.scale_slider.setValue(int(float(self.cfg.get("scale", 1.0)) * 100))
        self.scale_value = _label("", viz.INK, 11)
        self.scale_slider.valueChanged.connect(
            lambda v: self._apply("scale", v / 100.0)
        )
        form.addRow(_label("Size"), self._with_readout(self.scale_slider, self.scale_value))

        self.opacity_slider = QSlider(Qt.Horizontal)
        self.opacity_slider.setRange(20, 100)
        self.opacity_slider.setValue(int(float(self.cfg.get("opacity", 1.0)) * 100))
        self.opacity_value = _label("", viz.INK, 11)
        self.opacity_slider.valueChanged.connect(
            lambda v: self._apply("opacity", v / 100.0)
        )
        form.addRow(
            _label("Opacity"), self._with_readout(self.opacity_slider, self.opacity_value)
        )

        self.bg_combo = QComboBox()
        for text, value in (("Transparent", 0), ("Tinted", 190), ("Solid", 252)):
            self.bg_combo.addItem(text, value)
        self.bg_combo.currentIndexChanged.connect(
            lambda _i: self._apply("bg_alpha", self.bg_combo.currentData())
        )
        form.addRow(_label("Background"), self.bg_combo)

        self.fan_combo = QComboBox()
        for text, value in (("RPM", "rpm"), ("Duty %", "duty")):
            self.fan_combo.addItem(text, value)
        self.fan_combo.currentIndexChanged.connect(
            lambda _i: self._apply("fan_mode", self.fan_combo.currentData())
        )
        form.addRow(_label("Fan readout"), self.fan_combo)

        self.fan_max = QSpinBox()
        self.fan_max.setRange(1000, 20000)
        self.fan_max.setSingleStep(500)
        self.fan_max.setSuffix(" rpm")
        self.fan_max.valueChanged.connect(lambda v: self._apply("fan_max_rpm", v))
        form.addRow(_label("Fan sparkline full scale"), self.fan_max)
        layout.addWidget(appearance)

        behaviour = QGroupBox("Behaviour")
        bform = QFormLayout(behaviour)
        bform.setSpacing(9)

        self.interval_combo = QComboBox()
        for text, value in (("0.5 s", 500), ("1 s", 1000), ("2 s", 2000), ("5 s", 5000)):
            self.interval_combo.addItem(text, value)
        self.interval_combo.currentIndexChanged.connect(
            lambda _i: self._apply("interval_ms", self.interval_combo.currentData())
        )
        bform.addRow(_label("Update rate"), self.interval_combo)

        self.slow_every = QSpinBox()
        self.slow_every.setRange(1, 20)
        self.slow_every.setToolTip(
            "Poll nvidia-smi and the embedded controller every Nth tick. "
            "Higher is cheaper; the GPU and fan numbers update less often."
        )
        self.slow_every.valueChanged.connect(lambda v: self._apply("slow_every", v))
        bform.addRow(_label("Slow-sensor divisor"), self.slow_every)

        self.toggles: dict[str, QCheckBox] = {}
        for key, text, hint in (
            ("click_through", "Click-through",
             "Let clicks pass to whatever is underneath the tile"),
            ("hide_from_capture", "Hide from screenshots",
             "Keep the tile out of Print Screen, Snipping Tool and screen shares"),
            ("pop_on_alert", "Pop to front on alert",
             "Raise and flash when a session finishes or needs you"),
            ("show_stats", "Show stats line on the tile",
             "The one-line TODAY summary between the metrics and the agent rows"),
            ("locked", "Lock position", "Disable dragging"),
            ("auto_trim", "Trim memory automatically",
             "Free memory on its own once usage passes the limits below"),
        ):
            box = QCheckBox(text)
            box.setToolTip(hint)
            box.toggled.connect(lambda checked, k=key: self._apply(k, checked))
            self.toggles[key] = box
            bform.addRow("", box)

        self.auto_pct = QSpinBox()
        self.auto_pct.setRange(50, 99)
        self.auto_pct.setSuffix(" %")
        self.auto_pct.valueChanged.connect(lambda v: self._apply("auto_trim_pct", v))
        bform.addRow(_label("Auto-trim above"), self.auto_pct)

        self.auto_gb = QDoubleSpinBox()
        self.auto_gb.setRange(1.0, 256.0)
        self.auto_gb.setSingleStep(0.5)
        self.auto_gb.setSuffix(" GB")
        self.auto_gb.valueChanged.connect(lambda v: self._apply("auto_trim_gb", v))
        bform.addRow(_label("…or in use above"), self.auto_gb)
        layout.addWidget(behaviour)

        actions = QHBoxLayout()
        trim = QPushButton("Free memory now")
        trim.setEnabled(live)
        trim.clicked.connect(lambda: self.tile and self.tile.free_memory())
        actions.addWidget(trim)
        centre = QPushButton("Move tile to top-right")
        centre.setEnabled(live)
        centre.clicked.connect(self._reset_tile_position)
        actions.addWidget(centre)
        actions.addStretch(1)
        layout.addLayout(actions)
        layout.addStretch(1)

        self._loading_tile_tab = False
        self._load_tile_controls()
        page.setWidget(body)
        return page

    def _with_readout(self, slider: QSlider, readout: QLabel) -> QWidget:
        host = QWidget()
        row = QHBoxLayout(host)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(slider, 1)
        readout.setFixedWidth(46)
        row.addWidget(readout)
        return host

    def _load_tile_controls(self) -> None:
        """Push config into the controls without echoing back as changes."""
        self._loading_tile_tab = True
        try:
            cfg = self.tile.cfg if self.tile is not None else self.cfg
            self.scale_slider.setValue(int(float(cfg.get("scale", 1.0)) * 100))
            self.opacity_slider.setValue(int(float(cfg.get("opacity", 1.0)) * 100))
            alpha = int(cfg.get("bg_alpha", 190))
            index = self.bg_combo.findData(alpha)
            self.bg_combo.setCurrentIndex(index if index >= 0 else 1)
            index = self.fan_combo.findData(cfg.get("fan_mode", "rpm"))
            self.fan_combo.setCurrentIndex(max(0, index))
            self.fan_max.setValue(int(cfg.get("fan_max_rpm", 6000)))
            index = self.interval_combo.findData(int(cfg.get("interval_ms", 1000)))
            self.interval_combo.setCurrentIndex(index if index >= 0 else 1)
            self.slow_every.setValue(int(cfg.get("slow_every", 2)))
            for key, box in self.toggles.items():
                box.setChecked(bool(cfg.get(key, False)))
            self.auto_pct.setValue(int(cfg.get("auto_trim_pct", 90)))
            self.auto_gb.setValue(float(cfg.get("auto_trim_gb", 15.0)))
            self.scale_value.setText(f"{self.scale_slider.value()}%")
            self.opacity_value.setText(f"{self.opacity_slider.value()}%")
        finally:
            self._loading_tile_tab = False

    def _apply(self, key: str, value) -> None:
        if getattr(self, "_loading_tile_tab", False):
            return
        if key == "scale":
            self.scale_value.setText(f"{int(value * 100)}%")
        if key == "opacity":
            self.opacity_value.setText(f"{int(value * 100)}%")

        if self.tile is not None:
            if not self.tile.apply_setting(key, value):
                self._flash_footer(f"Could not apply {key} = {value!r}")
                return
            self.cfg = self.tile.cfg
        else:
            # Standalone: write the file the tile will read on next start.
            import widget as widget_module

            self.cfg[key] = value
            widget_module.save_config(self.cfg)
        self._flash_footer(f"{key} → {value}")

    def _reset_tile_position(self) -> None:
        if self.tile is None:
            return
        screen = QGuiApplication.primaryScreen().availableGeometry()
        self.tile.move(screen.right() - self.tile.width() - 12, screen.top() + 9)
        self.tile.cfg["x"], self.tile.cfg["y"] = self.tile.x(), self.tile.y()
        import widget as widget_module

        widget_module.save_config(self.tile.cfg)
        self._flash_footer("Tile moved to the top-right of the primary screen")

    # -- deploy tab --------------------------------------------------------

    def _build_deploy(self) -> QWidget:
        page = QScrollArea()
        page.setWidgetResizable(True)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(2, 8, 2, 8)
        layout.setSpacing(4)

        intro = _label(
            "Teammates run a one-line installer that wires the same five Claude "
            "Code hooks into their machine and reports to your Supabase project. "
            "Their laptops get a key that can write and not read; reading the "
            "board needs the viewer sign-in below.",
            viz.INK_DIM,
            11,
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        project = QGroupBox("1. Supabase project")
        pform = QFormLayout(project)
        pform.setSpacing(9)
        cfg = identity.team_config()

        self.url_edit = QLineEdit(str(cfg.get("url") or ""))
        self.url_edit.setPlaceholderText("https://yourproject.supabase.co")
        pform.addRow(_label("Project URL"), self.url_edit)

        self.key_edit = QLineEdit(str(cfg.get("anon_key") or ""))
        self.key_edit.setPlaceholderText("anon / publishable key")
        self.key_edit.setEchoMode(QLineEdit.Password)
        show_key = QCheckBox("show")
        show_key.toggled.connect(
            lambda on: self.key_edit.setEchoMode(
                QLineEdit.Normal if on else QLineEdit.Password
            )
        )
        key_row = QWidget()
        key_layout = QHBoxLayout(key_row)
        key_layout.setContentsMargins(0, 0, 0, 0)
        key_layout.addWidget(self.key_edit, 1)
        key_layout.addWidget(show_key)
        pform.addRow(_label("Anon key"), key_row)

        self.team_edit = QLineEdit(str(cfg.get("team_id") or ""))
        self.team_edit.setPlaceholderText("a short name, e.g. acme")
        pform.addRow(_label("Team id"), self.team_edit)

        self.member_edit = QLineEdit(str(cfg.get("member") or ""))
        self.member_edit.setPlaceholderText(identity.member_name())
        pform.addRow(_label("This laptop reports as"), self.member_edit)

        schema_note = _label(
            "Run schema.sql in the Supabase SQL editor once before enrolling "
            "anyone — it creates the tables and the write-only policies.",
            viz.INK_MUTED,
            10,
        )
        schema_note.setWordWrap(True)
        pform.addRow("", schema_note)

        buttons = QHBoxLayout()
        test = QPushButton("Test connection")
        test.clicked.connect(self._test_connection)
        buttons.addWidget(test)
        save = QPushButton("Save and start reporting")
        save.clicked.connect(self._save_enrollment)
        buttons.addWidget(save)
        copy_schema = QPushButton("Copy schema.sql")
        copy_schema.clicked.connect(self._copy_schema)
        buttons.addWidget(copy_schema)
        stop = QPushButton("Stop reporting")
        stop.clicked.connect(self._unenroll)
        buttons.addWidget(stop)
        buttons.addStretch(1)
        pform.addRow("", self._wrap(buttons))
        layout.addWidget(project)

        shares = QGroupBox("2. What this laptop shares")
        sform = QVBoxLayout(shares)
        self.share_boxes: dict[str, QCheckBox] = {}
        for key, text, hint in (
            ("share_tasks", "Finished tasks and token counts",
             "The per-turn records the whole dashboard is built on"),
            ("share_prompts", "The text of each prompt",
             "What people typed. Off means counts without content."),
            ("share_sessions", "Live session states",
             "Working / blocked / done, so the board shows who needs help now"),
            ("share_health", "Machine health",
             "CPU, RAM, GPU and temperatures, to spot a throttling laptop"),
        ):
            box = QCheckBox(text)
            box.setToolTip(hint)
            box.setChecked(bool(cfg.get(key, True)))
            box.toggled.connect(lambda _c, k=key: self._save_shares())
            self.share_boxes[key] = box
            sform.addWidget(box)
        note = _label(
            "These apply to this laptop only. The installer sets each "
            "teammate's defaults, and they can change their own afterwards.",
            viz.INK_MUTED,
            10,
        )
        note.setWordWrap(True)
        sform.addWidget(note)
        layout.addWidget(shares)

        viewer = QGroupBox("3. Viewer sign-in (to read the team board)")
        vform = QFormLayout(viewer)
        vform.setSpacing(9)
        self.email_edit = QLineEdit(cloud.Backend.from_team_config().viewer_email())
        self.email_edit.setPlaceholderText("you@company.com")
        vform.addRow(_label("Email"), self.email_edit)
        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.Password)
        self.password_edit.returnPressed.connect(self._sign_in)
        vform.addRow(_label("Password"), self.password_edit)
        vrow = QHBoxLayout()
        sign_in = QPushButton("Sign in")
        sign_in.clicked.connect(self._sign_in)
        vrow.addWidget(sign_in)
        sign_out = QPushButton("Sign out")
        sign_out.clicked.connect(self._sign_out)
        vrow.addWidget(sign_out)
        vrow.addStretch(1)
        self.viewer_state = _label("", viz.INK_MUTED, 10)
        vrow.addWidget(self.viewer_state)
        vform.addRow("", self._wrap(vrow))
        account_note = _label(
            "A Supabase Auth user, created under Authentication → Users, "
            "then granted read access with:\n"
            "  insert into team_members (user_id, team_id)\n"
            "  select id, '<team>' from auth.users where email = '<address>';",
            viz.INK_MUTED,
            10,
        )
        account_note.setWordWrap(True)
        vform.addRow("", account_note)
        layout.addWidget(viewer)

        install = QGroupBox("4. Install on a teammate's laptop")
        iform = QVBoxLayout(install)
        iform.setSpacing(8)

        mode_row = QHBoxLayout()
        mode_row.addWidget(_label("Install as"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Reporter only (hooks + sync, no tile)", "reporter")
        self.mode_combo.addItem("Full (tile, dashboard and sync)", "full")
        self.mode_combo.currentIndexChanged.connect(lambda _i: self._refresh_command())
        mode_row.addWidget(self.mode_combo, 1)
        iform.addLayout(mode_row)
        mode_note = _label(
            "Reporter-only needs Python and psutil and nothing else — no "
            "PySide6, no window, nothing on their screen. Full gives them the "
            "tile and their own dashboard too.",
            viz.INK_MUTED,
            10,
        )
        mode_note.setWordWrap(True)
        iform.addWidget(mode_note)

        self.command_box = QPlainTextEdit()
        self.command_box.setReadOnly(True)
        self.command_box.setFixedHeight(104)
        self.command_box.setStyleSheet(
            f"QPlainTextEdit {{ background: {viz.SURFACE_RAISED};"
            f" border: 1px solid {viz.BORDER}; border-radius: 6px;"
            f" color: {viz.INK}; font-family: Consolas, monospace;"
            " font-size: 11px; padding: 8px; }"
        )
        iform.addWidget(self.command_box)

        irow = QHBoxLayout()
        copy_cmd = QPushButton("Copy command")
        copy_cmd.clicked.connect(self._copy_command)
        irow.addWidget(copy_cmd)
        bundle = QPushButton("Build install bundle…")
        bundle.setToolTip(
            "Write a folder containing the agent files, their team.json and "
            "install.ps1, ready to hand over or drop on a share"
        )
        bundle.clicked.connect(self._build_bundle)
        irow.addWidget(bundle)
        irow.addStretch(1)
        iform.addLayout(irow)
        layout.addWidget(install)

        self.deploy_state = _label("", viz.INK_MUTED, 10)
        self.deploy_state.setWordWrap(True)
        layout.addWidget(self.deploy_state)
        layout.addStretch(1)

        self._refresh_command()
        self._refresh_viewer_state()
        page.setWidget(body)
        return page

    def _wrap(self, layout) -> QWidget:
        host = QWidget()
        host.setLayout(layout)
        return host

    def _form_values(self) -> dict:
        cfg = identity.team_config()
        cfg.update(
            {
                "url": self.url_edit.text().strip().rstrip("/"),
                "anon_key": self.key_edit.text().strip(),
                "team_id": self.team_edit.text().strip(),
                "member": self.member_edit.text().strip(),
            }
        )
        for key, box in self.share_boxes.items():
            cfg[key] = box.isChecked()
        return cfg

    def _test_connection(self) -> None:
        cfg = self._form_values()
        backend = cloud.Backend(
            url=cfg["url"], anon_key=cfg["anon_key"], team_id=cfg["team_id"]
        )
        result = backend.check()
        if result.ok:
            self.deploy_state.setText(
                "Project reachable and the key is accepted. Save to start reporting."
            )
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['done']};")
        else:
            self.deploy_state.setText(f"Could not reach the project — {result.error}")
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['needs_input']};")

    def _save_enrollment(self) -> None:
        cfg = self._form_values()
        missing = [
            name
            for name, value in (
                ("Project URL", cfg["url"]),
                ("Anon key", cfg["anon_key"]),
                ("Team id", cfg["team_id"]),
            )
            if not value
        ]
        if missing:
            self.deploy_state.setText("Still needed: " + ", ".join(missing))
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['needs_input']};")
            return
        cfg["enabled"] = True
        identity.save_team_config(cfg)
        self.deploy_state.setText(
            f"Enrolled as “{identity.member_name()}” in team "
            f"“{cfg['team_id']}”. Reporting starts on the next sync."
        )
        self.deploy_state.setStyleSheet(f"color: {viz.STATUS['done']};")
        self._refresh_command()
        self.refresh()

    def _save_shares(self) -> None:
        cfg = identity.team_config()
        for key, box in self.share_boxes.items():
            cfg[key] = box.isChecked()
        identity.save_team_config(cfg)
        self._flash_footer("Sharing preferences saved")

    def _unenroll(self) -> None:
        if (
            QMessageBox.question(
                self,
                "Stop reporting",
                "Delete this laptop's team.json? It stops reporting at once. "
                "Rows already sent stay in the project, and re-enrolling "
                "rejoins under the same device id.",
            )
            != QMessageBox.Yes
        ):
            return
        identity.unenroll()
        self.deploy_state.setText("Reporting stopped. team.json deleted.")
        self.deploy_state.setStyleSheet(f"color: {viz.INK_DIM};")
        self.refresh()

    def _copy_schema(self) -> None:
        path = os.path.join(HERE, "schema.sql")
        try:
            with open(path, encoding="utf-8") as fh:
                QGuiApplication.clipboard().setText(fh.read())
        except OSError as exc:
            self.deploy_state.setText(f"Could not read schema.sql: {exc}")
            return
        self.deploy_state.setText(
            "schema.sql copied. Paste it into the Supabase SQL editor and run it."
        )
        self.deploy_state.setStyleSheet(f"color: {viz.STATUS['done']};")

    def _sign_in(self) -> None:
        cfg = self._form_values()
        backend = cloud.Backend(
            url=cfg["url"], anon_key=cfg["anon_key"], team_id=cfg["team_id"]
        )
        result = backend.sign_in(
            self.email_edit.text().strip(), self.password_edit.text()
        )
        self.password_edit.clear()
        if result.ok:
            self.deploy_state.setText("Signed in. The Team tab can now read the board.")
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['done']};")
            self._sync_now()
        else:
            self.deploy_state.setText(f"Sign-in failed — {result.error}")
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['needs_input']};")
        self._refresh_viewer_state()

    def _sign_out(self) -> None:
        cloud.Backend.from_team_config().sign_out()
        self._refresh_viewer_state()
        self._flash_footer("Viewer session cleared")

    def _refresh_viewer_state(self) -> None:
        backend = cloud.Backend.from_team_config()
        if backend.configured and backend.signed_in:
            self.viewer_state.setText(f"signed in as {backend.viewer_email() or 'viewer'}")
            self.viewer_state.setStyleSheet(f"color: {viz.STATUS['done']};")
        else:
            self.viewer_state.setText("not signed in")
            self.viewer_state.setStyleSheet(f"color: {viz.INK_MUTED};")

    def _install_command(self) -> str:
        cfg = self._form_values()
        mode = self.mode_combo.currentData()
        url = cfg["url"] or "<project-url>"
        key = cfg["anon_key"] or "<anon-key>"
        team = cfg["team_id"] or "<team-id>"
        source = HERE.replace("\\", "/")
        shares = " ".join(
            f"-{flag}:${str(bool(cfg.get(key_name, True))).lower()}"
            for flag, key_name in (
                ("SharePrompts", "share_prompts"),
                ("ShareHealth", "share_health"),
                ("ShareSessions", "share_sessions"),
            )
        )
        return (
            "# Run in PowerShell on the teammate's laptop.\n"
            f"powershell -ExecutionPolicy Bypass -File \"{source}/install/install.ps1\" `\n"
            f"  -Url \"{url}\" -AnonKey \"{key}\" -TeamId \"{team}\" `\n"
            f"  -Mode {mode} -Member \"$env:USERNAME\" {shares}"
        )

    def _refresh_command(self) -> None:
        self.command_box.setPlainText(self._install_command())

    def _copy_command(self) -> None:
        QGuiApplication.clipboard().setText(self._install_command())
        self.deploy_state.setText("Install command copied to the clipboard.")
        self.deploy_state.setStyleSheet(f"color: {viz.STATUS['done']};")

    def _build_bundle(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        cfg = self._form_values()
        if not (cfg["url"] and cfg["anon_key"] and cfg["team_id"]):
            self.deploy_state.setText(
                "Fill in the project URL, key and team id before building a bundle."
            )
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['needs_input']};")
            return
        target = QFileDialog.getExistingDirectory(
            self, "Where should the bundle go?", os.path.expanduser("~")
        )
        if not target:
            return
        try:
            import deploy

            path = deploy.build_bundle(
                target, cfg, mode=self.mode_combo.currentData()
            )
        except Exception as exc:  # noqa: BLE001
            self.deploy_state.setText(f"Bundle failed: {exc}")
            self.deploy_state.setStyleSheet(f"color: {viz.STATUS['needs_input']};")
            return
        self.deploy_state.setText(
            f"Bundle written to {path}. Hand the folder over and have them run "
            "install.ps1 from it — it carries their team.json already."
        )
        self.deploy_state.setStyleSheet(f"color: {viz.STATUS['done']};")
        try:
            subprocess.Popen(["explorer", os.path.normpath(path)])
        except OSError:
            pass

    # -- data --------------------------------------------------------------

    def _events(self) -> list[dict]:
        """The event log, read at most once per refresh.

        Three things want it each tick -- the task rows, the exact reply median
        and the footer's event count -- and re-reading a log that only grows
        three times every six seconds adds up on a machine this tool exists to
        keep an eye on.
        """
        if self._events_cache is None:
            try:
                self._events_cache = events.load()
            except Exception:  # noqa: BLE001
                self._events_cache = []
        return self._events_cache

    def _local_rows(self) -> list[dict]:
        try:
            return stats.task_rows(self._events(), stats.window_start(self.period))
        except Exception:  # noqa: BLE001 - never let a bad row kill the window
            return []

    def _team_rows(self) -> list[dict]:
        cache = sync.read_cache()
        start = stats.window_start(self.period)
        return [
            r for r in cache.get("tasks", []) if float(r.get("ended_at") or 0) >= start
        ]

    def refresh(self) -> None:
        self._events_cache = None
        self._rows = self._team_rows() if self.scope == "team" else self._local_rows()
        summary = stats.summarize_tasks(self._rows)

        self.tiles["tasks"].set(str(summary["tasks"]))
        self.tiles["tokens"].set(viz.fmt_tokens(summary["tokens"]))
        self.tiles["cost"].set(viz.fmt_cost(summary["cost"]))
        self.tiles["task_time"].set(viz.fmt_duration(summary["task_median"]))
        self.tiles["tools"].set(f"{summary['tools_per_task']:.0f}")

        if self.scope == "me":
            # The exact median is available for our own log, where every
            # individual wait is on disk; a synced row carries only its total.
            try:
                exact = stats.summarize(self._events(), stats.window_start(self.period))
                self.tiles["reply"].set(viz.fmt_duration(exact["reply_median"]))
                self.tiles["reply"].caption.setText("median reply")
            except Exception:  # noqa: BLE001
                self.tiles["reply"].set(viz.fmt_duration(summary["reply_mean"]))
        else:
            self.tiles["reply"].set(viz.fmt_duration(summary["reply_mean"]))
            self.tiles["reply"].caption.setText("avg reply")

        days = self.period or 1
        self.trend.value_key = "tokens"
        self.trend.set_points(stats.daily_series(self._rows, days))
        self.trend_title.setText(
            ("TOKENS PER HOUR TODAY" if self.period == 0 else "TOKENS PER DAY")
        )
        if self.period == 0:
            # One bar is not a trend. For today, the daily chart would show a
            # single column, so the hour buckets take its place.
            hourly = stats.hourly_series(self._rows)
            self.trend.set_points(
                [{"label": f"{b['hour']:02d}", **b} for b in hourly]
            )

        self.donut.set_rows(
            summary["model_rows"], viz.fmt_tokens(summary["tokens"]), "tokens"
        )
        self.project_bars.set_rows(summary["project_rows"])
        self.project_title.setText(
            "TOKENS BY PROJECT" if self.scope == "me" else "TOKENS BY PROJECT (TEAM)"
        )
        self.hours.set_buckets(stats.hourly_series(self._rows))

        self._fill_tasks()
        self._fill_team()
        self._refresh_footer()

    def _auto_refresh(self) -> None:
        if self.isVisible():
            self.refresh()

    def _refresh_footer(self) -> None:
        state = sync.status()
        bits = []
        if state["enrolled"]:
            bits.append(f"reporting as {state['member']} · team {state['team_id']}")
            if state["last_push"]:
                bits.append(f"pushed {viz.fmt_ago(time.time() - state['last_push'])}")
            if state["last_error"]:
                bits.append(f"last error: {state['last_error'][:90]}")
        else:
            bits.append("not enrolled — nothing leaves this laptop")
        bits.append(f"{len(self._events())} events logged")
        self.footer.setText("   ·   ".join(bits))
        self.sync_note.setText(
            viz.fmt_ago(time.time() - state["last_push"]) if state["last_push"] else ""
        )
        self.sync_button.setEnabled(state["enrolled"])

    def _flash_footer(self, text: str) -> None:
        self.footer.setText(text)
        QTimer.singleShot(4000, self._refresh_footer)

    def _sync_now(self) -> None:
        if self._thread is not None and self._thread.isRunning():
            return
        self.sync_button.setEnabled(False)
        self.sync_button.setText("Syncing…")
        self._thread = QThread(self)
        self._worker = _Worker(self.period or 7)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.finished.connect(self._sync_done)
        self._thread.start()

    def _sync_done(self, report: dict) -> None:
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
            self._thread = None
        self._worker = None
        self.sync_button.setText("Sync now")
        self.sync_button.setEnabled(True)
        if report.get("ok"):
            pulled = report.get("pulled")
            note = f"Synced — sent {report.get('tasks', 0)} tasks"
            if pulled is not None:
                note += f", team board has {pulled} tasks in window"
            self._flash_footer(note)
        else:
            self._flash_footer(f"Sync problem: {report.get('error', 'unknown')}")
        self.refresh()

    # -- window ------------------------------------------------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._load_tile_controls()
        self.refresh()
        # The dashboard shows the same token and cost figures the tile does, so
        # it honours the same "keep it out of screen shares" setting.
        try:
            import widget as widget_module

            widget_module.set_capture_exclusion(
                self, bool(self.cfg.get("hide_from_capture", True))
            )
        except Exception:  # noqa: BLE001
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            self._thread.wait(2000)
        super().closeEvent(event)


def main() -> int:
    """Open the dashboard on its own, without the tile."""
    from PySide6.QtWidgets import QApplication

    import widget as widget_module

    app = QApplication(sys.argv)
    app.setApplicationName("Agent Dashboard")
    window = DashboardWindow(widget_module.load_config(), tile=None)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
