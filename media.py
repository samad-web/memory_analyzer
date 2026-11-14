"""The system's current media session, for the Dynamic Island's now-playing.

All WinRT lives here. If the ``winrt-*`` packages are missing -- a dev checkout
without them, or an old frozen build -- ``MediaWatcher.available`` is False, the
watcher never starts, and the island simply shows no media strip. The tile runs
either way; nothing here may raise into the UI.

Reading what Spotify or a browser is playing needs WinRT
``Windows.Media.Control``; pywin32 cannot and Qt has no equivalent. This is the
one place the project's no-new-dependency rule is bent (see build-docs/MEMORY).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from PySide6.QtCore import QThread, Signal

try:
    from winrt.runtime import init_apartment, uninit_apartment
    from winrt.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as _Manager,
        GlobalSystemMediaTransportControlsSessionPlaybackStatus as _Status,
    )
    from winrt.windows.storage.streams import DataReader

    _AVAILABLE = True
except Exception:  # noqa: BLE001 - any import failure means "no media support"
    _AVAILABLE = False

POLL_SECONDS = 1.0


@dataclass
class NowPlaying:
    title: str = ""
    artist: str = ""
    playing: bool = False
    art: bytes | None = None
    can_play_pause: bool = False
    can_next: bool = False
    can_prev: bool = False

    @property
    def ident(self) -> tuple:
        """Change signature excluding art -- art is refetched only on a track
        change, so including its bytes here would defeat that."""
        return (
            self.title, self.artist, self.playing,
            self.can_play_pause, self.can_next, self.can_prev,
        )


class MediaWatcher(QThread):
    """Polls the current media session on its own asyncio loop and emits
    ``changed`` (a NowPlaying, or None when nothing is playing) only on a real
    change. Controls are scheduled onto that same loop from the UI thread."""

    changed = Signal(object)  # NowPlaying | None
    available = _AVAILABLE

    def __init__(self) -> None:
        super().__init__()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop = False
        self._mgr = None
        self._last_ident: tuple | None = None
        self._art_for: tuple | None = None   # (title, artist) the art belongs to
        self._art: bytes | None = None

    # -- thread --------------------------------------------------------------

    def run(self) -> None:
        if not _AVAILABLE:
            return
        try:
            init_apartment()
        except Exception:  # noqa: BLE001 - already initialised is fine
            pass
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._poll_forever())
        except Exception:  # noqa: BLE001 - a watcher glitch must not kill the tile
            pass
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                uninit_apartment()
            except Exception:  # noqa: BLE001
                pass

    async def _manager(self):
        if self._mgr is None:
            self._mgr = await _Manager.request_async()
        return self._mgr

    async def _poll_forever(self) -> None:
        while not self._stop:
            try:
                await self._poll_once()
            except Exception:  # noqa: BLE001 - transient WinRT failure, try again
                pass
            # Sleep in slices so stop() takes effect promptly.
            for _ in range(int(POLL_SECONDS * 10)):
                if self._stop:
                    return
                await asyncio.sleep(0.1)

    async def _poll_once(self) -> None:
        mgr = await self._manager()
        session = mgr.get_current_session()
        if session is None:
            if self._last_ident is not None:
                self._last_ident = None
                self._art_for = self._art = None
                self.changed.emit(None)
            return

        props = await session.try_get_media_properties_async()
        info = session.get_playback_info()
        controls = info.controls
        np = NowPlaying(
            title=(props.title or "").strip(),
            artist=(props.artist or "").strip(),
            playing=info.playback_status == _Status.PLAYING,
            can_play_pause=bool(
                controls.is_play_enabled or controls.is_pause_enabled
                or controls.is_play_pause_toggle_enabled
            ),
            can_next=bool(controls.is_next_enabled),
            can_prev=bool(controls.is_previous_enabled),
        )
        if np.ident == self._last_ident:
            return

        track = (np.title, np.artist)
        if track != self._art_for:
            self._art = await self._read_art(props)
            self._art_for = track
        np.art = self._art
        self._last_ident = np.ident
        self.changed.emit(np)

    async def _read_art(self, props) -> bytes | None:
        ref = props.thumbnail
        if ref is None:
            return None
        try:
            stream = await ref.open_read_async()
            size = stream.size
            if not size:
                return None
            reader = DataReader(stream)
            await reader.load_async(size)
            return bytes(reader.read_buffer(size))
        except Exception:  # noqa: BLE001 - no art is not an error
            return None

    # -- controls (called from the UI thread) --------------------------------

    def toggle_play_pause(self) -> None:
        self._control("try_toggle_play_pause_async")

    def next(self) -> None:
        self._control("try_skip_next_async")

    def previous(self) -> None:
        self._control("try_skip_previous_async")

    def _control(self, method: str) -> None:
        loop = self._loop
        if not _AVAILABLE or loop is None:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._do_control(method), loop)
        except Exception:  # noqa: BLE001 - loop may be closing
            pass

    async def _do_control(self, method: str) -> None:
        try:
            mgr = await self._manager()
            session = mgr.get_current_session()
            if session is not None:
                await getattr(session, method)()
        except Exception:  # noqa: BLE001 - a dead/again-busy session is not fatal
            pass

    def stop(self) -> None:
        self._stop = True
        loop = self._loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)  # wake the loop to exit
            except Exception:  # noqa: BLE001
                pass
        self.wait(2000)
