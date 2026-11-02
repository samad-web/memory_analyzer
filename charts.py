r"""Hand-painted chart widgets for the Qt dashboard.

Painted with ``QPainter`` rather than pulled in from a charting library, for
the same reason the tile is: the whole app is three small dependencies, and a
chart toolkit would be the largest thing in it by an order of magnitude for
four chart forms. It also keeps the dashboard looking like the tile.

Colours and formatters come from ``viz`` -- the web dashboard reads the same
ones, so the two stay in step.

The mark conventions here are deliberate and consistent across every chart:

* bars are anchored to the baseline and rounded only at the data end, so the
  length that encodes the value is never visually trimmed at its root;
* adjacent fills keep a 2px gap in the surface colour, which is what stops a
  run of bars reading as one mass;
* gridlines are recessive and unlabelled except at the top of the scale;
* values are labelled selectively -- the extremes and whatever is hovered --
  because a number over every bar is noise, not information;
* every chart has a hover layer. A chart on screen is interactive whether or
  not you planned for it, and "what exactly is that bar" is the first question
  anyone asks.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
)
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

import viz

# A bar shorter than this still gets drawn, so "a little" never looks like
# "none" -- a zero-height bar and a missing bar are different facts.
MIN_BAR_PX = 2
BAR_GAP = 2
CORNER = 4


def _c(hex_value: str, alpha: int = 255) -> QColor:
    colour = QColor(hex_value)
    colour.setAlpha(alpha)
    return colour


def _rounded_top(path: QPainterPath, rect: QRectF, radius: float) -> None:
    """A rect rounded at the top two corners only."""
    radius = max(0.0, min(radius, rect.width() / 2, rect.height()))
    path.moveTo(rect.left(), rect.bottom())
    path.lineTo(rect.left(), rect.top() + radius)
    path.quadTo(rect.left(), rect.top(), rect.left() + radius, rect.top())
    path.lineTo(rect.right() - radius, rect.top())
    path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
    path.lineTo(rect.right(), rect.bottom())
    path.closeSubpath()


def _rounded_right(path: QPainterPath, rect: QRectF, radius: float) -> None:
    """A rect rounded at the right two corners only."""
    radius = max(0.0, min(radius, rect.height() / 2, rect.width()))
    path.moveTo(rect.left(), rect.top())
    path.lineTo(rect.right() - radius, rect.top())
    path.quadTo(rect.right(), rect.top(), rect.right(), rect.top() + radius)
    path.lineTo(rect.right(), rect.bottom() - radius)
    path.quadTo(rect.right(), rect.bottom(), rect.right() - radius, rect.bottom())
    path.lineTo(rect.left(), rect.bottom())
    path.closeSubpath()


def _nice_ceiling(value: float) -> float:
    """A round number at or above ``value``, for the top gridline.

    An axis that tops out at the data's exact maximum puts the tallest bar
    flush against the frame and gives the eye nothing to read the others
    against.
    """
    if value <= 0:
        return 1.0
    import math

    exponent = math.floor(math.log10(value))
    base = 10.0**exponent
    for step in (1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.5, 10.0):
        if value <= step * base:
            return step * base
    return 10.0 * base


class ColumnChart(QWidget):
    """Vertical bars for a single series over time -- tokens or tasks per day.

    One series, so no legend: the caption names it. Only the busiest day and
    the hovered day carry a value label.
    """

    AXIS_H = 18
    TOP_PAD = 16

    def __init__(
        self,
        caption: str = "",
        value_key: str = "tokens",
        colour: str = viz.SERIES[0],
        formatter=viz.fmt_tokens,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.caption = caption
        self.value_key = value_key
        self.colour = colour
        self.formatter = formatter
        self.points: list[dict] = []
        self._hover = -1
        self.setMinimumHeight(140)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_points(self, points: list[dict]) -> None:
        self.points = list(points or [])
        self._hover = min(self._hover, len(self.points) - 1)
        self.update()

    # -- geometry ----------------------------------------------------------

    def _plot_rect(self) -> QRect:
        return QRect(
            0,
            self.TOP_PAD,
            self.width(),
            max(10, self.height() - self.TOP_PAD - self.AXIS_H),
        )

    def _index_at(self, x: int) -> int:
        if not self.points:
            return -1
        plot = self._plot_rect()
        slot = plot.width() / len(self.points)
        index = int((x - plot.left()) / slot) if slot > 0 else -1
        return index if 0 <= index < len(self.points) else -1

    # -- interaction -------------------------------------------------------

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(int(event.position().x()))
        if index != self._hover:
            self._hover = index
            self.update()
        if index >= 0:
            point = self.points[index]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{point.get('label', '')}\n"
                f"{self.formatter(point.get(self.value_key, 0))} "
                f"· {point.get('tasks', 0)} tasks",
                self,
            )
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        QToolTip.hideText()
        self.update()

    # -- paint -------------------------------------------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        plot = self._plot_rect()

        if not self.points:
            self._paint_empty(p)
            return

        peak = max(float(pt.get(self.value_key, 0) or 0) for pt in self.points)
        top = _nice_ceiling(peak)

        # Gridlines, labelled only at the top of the scale.
        p.setPen(QPen(_c(viz.GRID), 1))
        for fraction in (0.0, 0.5, 1.0):
            y = plot.bottom() - fraction * plot.height()
            p.drawLine(plot.left(), int(y), plot.right(), int(y))
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_c(viz.INK_MUTED))
        p.drawText(
            QRect(plot.left(), plot.top() - 14, plot.width(), 12),
            Qt.AlignRight | Qt.AlignVCenter,
            self.formatter(top),
        )

        slot = plot.width() / len(self.points)
        bar_w = max(2.0, slot - BAR_GAP)
        peak_index = max(
            range(len(self.points)),
            key=lambda i: float(self.points[i].get(self.value_key, 0) or 0),
        )

        for index, point in enumerate(self.points):
            value = float(point.get(self.value_key, 0) or 0)
            height = (value / top) * plot.height() if top else 0.0
            if value > 0:
                height = max(height, MIN_BAR_PX)
            x = plot.left() + index * slot + (slot - bar_w) / 2
            rect = QRectF(x, plot.bottom() - height, bar_w, height)

            hovered = index == self._hover
            fill = _c(self.colour, 255 if hovered else 216)
            if value <= 0:
                # An empty day is shown as a baseline tick, not a gap, so the
                # x-axis stays honest about the days it covers.
                p.setPen(QPen(_c(viz.GRID), 2))
                p.drawLine(
                    int(rect.left()), plot.bottom(), int(rect.right()), plot.bottom()
                )
                continue

            path = QPainterPath()
            _rounded_top(path, rect, CORNER)
            p.setPen(Qt.NoPen)
            p.setBrush(fill)
            p.drawPath(path)

            if hovered or index == peak_index:
                p.setFont(QFont("Consolas", 8, QFont.DemiBold))
                p.setPen(_c(viz.INK))
                p.drawText(
                    QRectF(rect.left() - 14, rect.top() - 15, rect.width() + 28, 13),
                    Qt.AlignCenter,
                    self.formatter(value),
                )

        # x labels: first, last and hovered, so they never collide.
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_c(viz.INK_MUTED))
        show = {0, len(self.points) - 1}
        if self._hover >= 0:
            show.add(self._hover)
        for index in sorted(show):
            label = str(self.points[index].get("label", ""))
            x = plot.left() + index * slot
            p.drawText(
                QRectF(x - slot, plot.bottom() + 3, slot * 3, 14),
                Qt.AlignCenter,
                label,
            )

    def _paint_empty(self, p: QPainter) -> None:
        p.setFont(QFont("Segoe UI", 9))
        p.setPen(_c(viz.INK_MUTED))
        p.drawText(self.rect(), Qt.AlignCenter, "No activity in this window yet")


class RankedBars(QWidget):
    """Horizontal bars, longest first -- projects, members, models.

    Horizontal because the labels are names: a vertical bar chart with eight
    project names underneath it either rotates them or truncates them, and both
    are worse than simply turning the chart on its side. Every bar is directly
    labelled with its name and value, so colour is never the only carrier of
    identity and no legend is needed.
    """

    ROW_H = 26
    LABEL_W = 132

    def __init__(
        self,
        value_key: str = "tokens",
        formatter=viz.fmt_tokens,
        colour_for=None,
        limit: int = 8,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.value_key = value_key
        self.formatter = formatter
        self.colour_for = colour_for
        self.limit = limit
        self.rows: list[dict] = []
        self._hover = -1
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)

    def set_rows(self, rows: list[dict]) -> None:
        """Takes the full list and keeps the top ``limit``, folding the rest.

        The tail becomes one "other" row rather than being dropped, so the bars
        still add up to the total shown in the stat tiles above them.
        """
        rows = sorted(
            list(rows or []),
            key=lambda r: float(r.get(self.value_key, 0) or 0),
            reverse=True,
        )
        if len(rows) > self.limit:
            tail = rows[self.limit - 1 :]
            folded = {
                "name": f"other ({len(tail)})",
                self.value_key: sum(float(r.get(self.value_key, 0) or 0) for r in tail),
                "tasks": sum(int(r.get("tasks", 0) or 0) for r in tail),
                "_folded": True,
            }
            rows = rows[: self.limit - 1] + [folded]
        self.rows = rows
        self.setMinimumHeight(max(40, len(self.rows) * self.ROW_H + 8))
        self._hover = min(self._hover, len(self.rows) - 1)
        self.updateGeometry()
        self.update()

    def _index_at(self, y: int) -> int:
        index = int((y - 4) / self.ROW_H)
        return index if 0 <= index < len(self.rows) else -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(int(event.position().y()))
        if index != self._hover:
            self._hover = index
            self.update()
        if index >= 0:
            row = self.rows[index]
            detail = [f"{row.get('name', '')}"]
            detail.append(f"{self.formatter(row.get(self.value_key, 0))}")
            if row.get("tasks"):
                detail.append(f"{row['tasks']} tasks")
            if row.get("cost"):
                detail.append(viz.fmt_cost(row["cost"]))
            QToolTip.showText(
                event.globalPosition().toPoint(), "  ·  ".join(detail), self
            )
        else:
            QToolTip.hideText()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        QToolTip.hideText()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        if not self.rows:
            p.setFont(QFont("Segoe UI", 9))
            p.setPen(_c(viz.INK_MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "Nothing to show yet")
            return

        top_value = max(
            float(r.get(self.value_key, 0) or 0) for r in self.rows
        ) or 1.0
        metrics = QFontMetrics(QFont("Segoe UI", 9))
        value_w = 76
        track_left = self.LABEL_W + 8
        track_w = max(20, self.width() - track_left - value_w - 4)

        for index, row in enumerate(self.rows):
            y = 4 + index * self.ROW_H
            value = float(row.get(self.value_key, 0) or 0)
            hovered = index == self._hover

            if hovered:
                p.setPen(Qt.NoPen)
                p.setBrush(_c(viz.SURFACE_RAISED))
                p.drawRoundedRect(
                    QRectF(0, y - 1, self.width(), self.ROW_H - 2), 5, 5
                )

            # Name, elided rather than overflowing into the track.
            p.setFont(QFont("Segoe UI", 9))
            p.setPen(_c(viz.INK if hovered else viz.INK_DIM))
            p.drawText(
                QRect(4, y, self.LABEL_W, self.ROW_H - BAR_GAP),
                Qt.AlignLeft | Qt.AlignVCenter,
                metrics.elidedText(
                    str(row.get("name", "")), Qt.ElideRight, self.LABEL_W - 8
                ),
            )

            # Track, then the bar on top of it.
            bar_h = 10.0
            bar_y = y + (self.ROW_H - BAR_GAP - bar_h) / 2
            p.setPen(Qt.NoPen)
            p.setBrush(_c(viz.GRID))
            p.drawRoundedRect(QRectF(track_left, bar_y, track_w, bar_h), 4, 4)

            length = (value / top_value) * track_w
            if value > 0:
                length = max(length, MIN_BAR_PX)
            if length > 0:
                colour = (
                    viz.INK_MUTED
                    if row.get("_folded")
                    else (
                        self.colour_for(row, index)
                        if self.colour_for
                        else viz.series_color(index)
                    )
                )
                path = QPainterPath()
                _rounded_right(path, QRectF(track_left, bar_y, length, bar_h), CORNER)
                p.setBrush(_c(colour, 255 if hovered else 224))
                p.drawPath(path)

            p.setFont(QFont("Consolas", 9))
            p.setPen(_c(viz.INK if hovered else viz.INK_DIM))
            p.drawText(
                QRect(self.width() - value_w - 2, y, value_w, self.ROW_H - BAR_GAP),
                Qt.AlignRight | Qt.AlignVCenter,
                self.formatter(value),
            )


class HourStrip(QWidget):
    """Tokens by hour of day, as a sequential heat strip.

    Twenty-four values is too many bars to read as bars in this width and too
    few to need a full heatmap, so it is one row of cells on a single-hue ramp:
    continuous magnitude, which is what a sequential encoding is for.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.buckets: list[dict] = []
        self._hover = -1
        self.setFixedHeight(46)
        self.setMouseTracking(True)

    def set_buckets(self, buckets: list[dict]) -> None:
        self.buckets = list(buckets or [])
        self.update()

    def _index_at(self, x: int) -> int:
        if not self.buckets:
            return -1
        slot = self.width() / len(self.buckets)
        index = int(x / slot) if slot else -1
        return index if 0 <= index < len(self.buckets) else -1

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        index = self._index_at(int(event.position().x()))
        if index != self._hover:
            self._hover = index
            self.update()
        if index >= 0:
            bucket = self.buckets[index]
            QToolTip.showText(
                event.globalPosition().toPoint(),
                f"{bucket['hour']:02d}:00  ·  "
                f"{viz.fmt_tokens(bucket.get('tokens', 0))}  ·  "
                f"{bucket.get('tasks', 0)} tasks",
                self,
            )

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = -1
        QToolTip.hideText()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        if not self.buckets:
            return

        peak = max(float(b.get("tokens", 0) or 0) for b in self.buckets) or 1.0
        slot = self.width() / len(self.buckets)
        cell_w = max(2.0, slot - BAR_GAP)

        for index, bucket in enumerate(self.buckets):
            value = float(bucket.get("tokens", 0) or 0)
            x = index * slot + (slot - cell_w) / 2
            rect = QRectF(x, 4, cell_w, 22)
            p.setPen(Qt.NoPen)
            if value <= 0:
                p.setBrush(_c(viz.GRID))
            else:
                p.setBrush(_c(viz.sequential_color(value / peak)))
            p.drawRoundedRect(rect, 3, 3)
            if index == self._hover:
                p.setPen(QPen(_c(viz.INK), 1.5))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(-1, -1, 1, 1), 4, 4)

        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_c(viz.INK_MUTED))
        for hour in (0, 6, 12, 18):
            p.drawText(
                QRectF(hour * slot - slot, 28, slot * 3, 14),
                Qt.AlignCenter,
                f"{hour:02d}",
            )


class Meter(QWidget):
    """A labelled percentage bar -- one machine-health reading.

    This is where the tile's accent colours are reused: each meter sits on its
    own row with its own name beside it, so the colour is decoration on top of
    a text label rather than the thing you have to decode.
    """

    def __init__(
        self,
        label: str,
        colour: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.label = label
        self.colour = colour
        self.fraction = 0.0
        self.readout = "--"
        self.setFixedHeight(18)

    def set_value(self, fraction: float | None, readout: str) -> None:
        self.fraction = max(0.0, min(1.0, float(fraction or 0.0)))
        self.readout = readout
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_c(viz.INK_MUTED))
        p.drawText(QRect(0, 0, 30, self.height()), Qt.AlignLeft | Qt.AlignVCenter, self.label)

        track = QRectF(32, self.height() / 2 - 3.5, max(10, self.width() - 32 - 70), 7)
        p.setPen(Qt.NoPen)
        p.setBrush(_c(viz.GRID))
        p.drawRoundedRect(track, 3, 3)
        if self.fraction > 0:
            length = max(MIN_BAR_PX, track.width() * self.fraction)
            path = QPainterPath()
            _rounded_right(
                path, QRectF(track.left(), track.top(), length, track.height()), 3
            )
            p.setBrush(_c(self.colour))
            p.drawPath(path)

        p.setFont(QFont("Consolas", 8))
        p.setPen(_c(viz.INK_DIM))
        p.drawText(
            QRect(self.width() - 68, 0, 66, self.height()),
            Qt.AlignRight | Qt.AlignVCenter,
            self.readout,
        )


class Donut(QWidget):
    """Token split by model family.

    A part-to-whole across at most five slices, which is the one case a ring
    beats bars: the question is "how much of the spend is Opus", a proportion,
    and the ring puts the total in the middle where it is also the answer to
    "how much in total". Slices carry a 2px surface gap and the legend is drawn
    beside it with names, so no slice depends on colour alone.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.rows: list[dict] = []
        self.total_label = ""
        self.caption = ""
        self.setMinimumHeight(132)
        self.setMouseTracking(True)
        self._hover = -1

    def set_rows(self, rows: list[dict], total_label: str, caption: str = "") -> None:
        self.rows = [r for r in (rows or []) if float(r.get("tokens", 0) or 0) > 0]
        self.total_label = total_label
        self.caption = caption
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)

        if not self.rows:
            p.setFont(QFont("Segoe UI", 9))
            p.setPen(_c(viz.INK_MUTED))
            p.drawText(self.rect(), Qt.AlignCenter, "No tokens in this window")
            return

        total = sum(float(r.get("tokens", 0) or 0) for r in self.rows) or 1.0
        size = min(self.height() - 8, 112)
        ring = QRectF(4, (self.height() - size) / 2, size, size)
        thickness = max(10.0, size * 0.17)

        start = 90 * 16
        for index, row in enumerate(self.rows):
            fraction = float(row.get("tokens", 0) or 0) / total
            span = -int(round(fraction * 360 * 16))
            colour = _c(viz.model_color(row.get("name", "")))
            pen = QPen(colour, thickness)
            pen.setCapStyle(Qt.FlatCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            inset = thickness / 2 + 1
            p.drawArc(ring.adjusted(inset, inset, -inset, -inset), start, span)
            start += span

        # 2px surface-coloured separators between slices, drawn after the arcs
        # so a thin slice cannot be swallowed by its neighbour.
        if len(self.rows) > 1:
            start = 90 * 16
            for row in self.rows:
                fraction = float(row.get("tokens", 0) or 0) / total
                pen = QPen(_c(viz.SURFACE_CARD), 2)
                pen.setCapStyle(Qt.FlatCap)
                p.setPen(pen)
                inset = thickness / 2 + 1
                p.drawArc(ring.adjusted(inset, inset, -inset, -inset), start, -8)
                start -= int(round(fraction * 360 * 16))

        p.setFont(QFont("Consolas", 13, QFont.DemiBold))
        p.setPen(_c(viz.INK))
        p.drawText(
            QRectF(ring.left(), ring.center().y() - 14, ring.width(), 18),
            Qt.AlignCenter,
            self.total_label,
        )
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(_c(viz.INK_MUTED))
        p.drawText(
            QRectF(ring.left(), ring.center().y() + 3, ring.width(), 14),
            Qt.AlignCenter,
            self.caption or "tokens",
        )

        # Legend: swatch, name, share. Always present -- two or more series.
        legend_x = ring.right() + 18
        row_h = 19
        top = (self.height() - len(self.rows) * row_h) / 2
        for index, row in enumerate(self.rows):
            y = top + index * row_h
            share = float(row.get("tokens", 0) or 0) / total
            p.setPen(Qt.NoPen)
            p.setBrush(_c(viz.model_color(row.get("name", ""))))
            p.drawRoundedRect(QRectF(legend_x, y + 5, 9, 9), 2, 2)
            p.setFont(QFont("Segoe UI", 9))
            p.setPen(_c(viz.INK_DIM))
            p.drawText(
                QRectF(legend_x + 15, y, 86, row_h),
                Qt.AlignLeft | Qt.AlignVCenter,
                str(row.get("name", "")),
            )
            p.setFont(QFont("Consolas", 9))
            p.setPen(_c(viz.INK))
            p.drawText(
                QRectF(legend_x + 101, y, max(40, self.width() - legend_x - 105), row_h),
                Qt.AlignRight | Qt.AlignVCenter,
                f"{share * 100:.0f}%",
            )
