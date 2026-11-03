r"""The installable shell: manifest and icons.

A web app manifest needs real raster icons -- a browser will refuse to offer
installation without at least a 192 and a 512, and iOS wants a PNG for the
home screen. There is no imaging library here and "no new runtime
dependencies" is a constraint on the board, so the PNGs are written directly.

That sounds worse than it is. A PNG is a signature, three length-tagged chunks
and a CRC each, with the pixels zlib-deflated -- and `zlib`, `struct` and
`binascii.crc32` are all standard library. The whole encoder is about twenty
lines, it is exercised by decoding its own output back to the pixels that went
in, and nothing about it is guesswork.

Generated rather than committed, so the icon follows ``viz`` the way every
other surface does instead of going stale the first time the palette moves.
"""

from __future__ import annotations

import binascii
import struct
import zlib

import viz

NAME = "Agent Dashboard"
SHORT_NAME = "Agents"
ICON_SIZES = (192, 512)

# Drawn as a share of the canvas so one description works at every size.
_PAD = 0.18            # margin around the bars
_BARS = (0.45, 0.78, 0.60)
_GAP = 0.09


def _rgb(colour: str) -> tuple[int, int, int]:
    colour = str(colour or "").lstrip("#")
    if len(colour) == 3:
        colour = "".join(c * 2 for c in colour)
    if len(colour) != 6:
        return (0, 0, 0)
    return tuple(int(colour[i:i + 2], 16) for i in (0, 2, 4))


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", binascii.crc32(tag + data) & 0xFFFFFFFF))


def png(pixels: list[list[tuple[int, int, int, int]]]) -> bytes:
    """Encode RGBA rows as a PNG.

    Filter type 0 on every scanline: the images are flat colour, so a
    predictor would save a few hundred bytes and add the only part of this
    that could be subtly wrong.
    """
    height = len(pixels)
    width = len(pixels[0]) if height else 0
    raw = bytearray()
    for row in pixels:
        raw.append(0)
        for r, g, b, a in row:
            raw += bytes((r, g, b, a))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6,
                                          0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def icon(size: int = 192) -> bytes:
    """The app icon: three bars on the dashboard's own background.

    The same shape the tile draws, so the thing on the home screen looks like
    the thing it opens.
    """
    size = max(16, min(1024, int(size)))
    background = _rgb(viz.SURFACE)
    bar_colours = [_rgb(c) for c in
                   (viz.STATUS["working"], viz.STATUS["needs_input"],
                    viz.STATUS["done"])]

    pad = size * _PAD
    span = size - 2 * pad
    gap = span * _GAP
    thickness = (span - gap * (len(_BARS) - 1)) / len(_BARS)

    rows = []
    for y in range(size):
        row = []
        for x in range(size):
            pixel = background + (255,)
            offset = y - pad
            if 0 <= offset < span:
                index = int(offset // (thickness + gap))
                within = offset - index * (thickness + gap)
                if 0 <= index < len(_BARS) and within < thickness:
                    if pad <= x < pad + span * _BARS[index]:
                        pixel = bar_colours[index] + (255,)
            row.append(pixel)
        rows.append(row)
    return png(rows)


def manifest(start_url: str = "/") -> dict:
    """The web app manifest.

    ``display: standalone`` is the point of the exercise -- without it a phone
    opens a browser tab with an address bar, which is not an app and which
    iOS will not deliver a push to.
    """
    return {
        "name": NAME,
        "short_name": SHORT_NAME,
        "description": "Agent activity, memory and escalations for this machine.",
        "start_url": start_url,
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": viz.SURFACE,
        "theme_color": viz.SURFACE,
        "icons": [
            {
                "src": "/icon-%d.png" % size,
                "sizes": "%dx%d" % (size, size),
                "type": "image/png",
                # "any maskable" so Android may crop it to the system shape;
                # the bars sit inside the 18% margin, so a crop cannot eat
                # them.
                "purpose": "any maskable",
            }
            for size in ICON_SIZES
        ],
    }
