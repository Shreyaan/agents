from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterable, Literal

from livekit import rtc

from .. import utils
from .log import logger

EventTypes = Literal["playout_started", "playout_stopped"]


class PlayoutHandle:
    def __init__(self, playout_source: AsyncIterable[rtc.AudioFrame]) -> None:
        self._playout_source = playout_source
        self._interrupted = False
        self._done_fut = asyncio.Future()

    @property
    def interrupted(self) -> bool:
        return self._interrupted

    @property
    def playing(self) -> bool:
        return not self._done_fut.done()

    def interrupt(self) -> None:
        if not self.playing:
            return

        self._interrupted = True

    def __await__(self):
        return self._done_fut.__await__()


class CancellableAudioSource(utils.EventEmitter[EventTypes]):
    def __init__(self, *, source: rtc.AudioSource, alpha: float = 0.95) -> None:
        super().__init__()
        self._source = source
        self._target_volume, self._smoothed_volume = 1.0, 1.0
        self._vol_filter = utils.ExpFilter(alpha=alpha)
        self._playout_atask: asyncio.Task[None] | None = None
        self._closed = False

    @property
    def target_volume(self) -> float:
        return self._target_volume

    @target_volume.setter
    def target_volume(self, value: float) -> None:
        self._target_volume = value

    async def aclose(self) -> None:
        if self._closed:
            return

        self._closed = True

        if self._playout_atask is not None:
            await self._playout_atask

    def play(self, playout_source: AsyncIterable[rtc.AudioFrame]) -> PlayoutHandle:
        if self._closed:
            raise ValueError("cancellable source is closed")

        handle = PlayoutHandle(playout_source=playout_source)
        self._playout_atask = asyncio.create_task(
            self._playout_task(self._playout_atask, handle)
        )
        return handle
    
    @utils.log_exceptions(logger=logger)
    async def _playout_task(
        self,
        old_task: asyncio.Task[None] | None,
        handle: PlayoutHandle,
    ) -> None:
        def _should_break():
            eps = 1e-6
            return handle.interrupted and self._vol_filter.filtered() <= eps

        first_frame = True
        cancelled = False

        try:
            if old_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    old_task.cancel()
                    await old_task

            async for frame in handle._playout_source:
                if first_frame:
                    self.emit("playout_started")
                    first_frame = False

                if _should_break():
                    cancelled = True
                    break

                # divide the frame by chunks of 20ms
                ms20 = frame.sample_rate // 100
                i = 0
                while i < len(frame.data):
                    if _should_break():
                        cancelled = True
                        break

                    print("frame.data", frame.data, "volume", self._vol_filter.filtered())

                    rem = min(ms20, len(frame.data) - i)
                    data = frame.data[i : i + rem]
                    i += rem

                    tv = self._target_volume if not handle.interrupted else 0.0
                    dt = 1 / len(data)
                    for si in range(0, len(data)):
                        vol = self._vol_filter.apply(dt, tv)
                        data[si] = int((data[si] / 32768) * vol * 32768)

                    chunk_frame = rtc.AudioFrame(
                        data=data.tobytes(),
                        sample_rate=frame.sample_rate,
                        num_channels=frame.num_channels,
                        samples_per_channel=rem,
                    )
                    await self._source.capture_frame(chunk_frame)
        finally:
            if not first_frame:
                self.emit("playout_stopped", cancelled)

            handle._done_fut.set_result(None)
