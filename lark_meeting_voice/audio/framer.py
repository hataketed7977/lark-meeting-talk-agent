"""PCM chunking + pacing for Feishu upstream.

Per the meeting endpoint spec:
  - 24 kHz, s16le, mono => 48 bytes/ms
  - Each upstream frame should be 20-100 ms and <= 8000 bytes.
  - delta length MUST be even (don't split a sample).
"""

from __future__ import annotations

import asyncio

# 24kHz s16le mono = 48 B/ms.
BYTES_PER_MS = 48
FRAME_MS = 100
FRAME_BYTES = BYTES_PER_MS * FRAME_MS  # 4800
MAX_FRAME_BYTES = 8000

assert FRAME_BYTES % 2 == 0
assert FRAME_BYTES <= MAX_FRAME_BYTES


def split_pcm(pcm: bytes, frame_bytes: int = FRAME_BYTES) -> list[bytes]:
    """Split raw PCM into even-length chunks of `frame_bytes`."""
    if frame_bytes % 2 != 0:
        raise ValueError("frame_bytes must be even (s16le sample boundary)")
    chunks = [pcm[i : i + frame_bytes] for i in range(0, len(pcm), frame_bytes)]
    if chunks and len(chunks[-1]) % 2 != 0:
        # Drop the dangling byte to preserve sample alignment.
        chunks[-1] = chunks[-1][:-1]
    return [c for c in chunks if c]


class PacedSender:
    """Calls `send_fn(chunk)` once per FRAME_MS, regardless of input arrival pattern.

    Used to feed Feishu upstream at real-time speed, even when the TTS service
    bursts a lot of audio at once.
    """

    def __init__(self, send_fn, frame_ms: int = FRAME_MS) -> None:
        self._send_fn = send_fn
        self._frame_ms = frame_ms
        self._frame_bytes = frame_ms * BYTES_PER_MS
        self._q: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=256)
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()
        self._buffer = bytearray()
        self._drained = asyncio.Event()
        self._drained.set()

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="paced-sender")

    async def feed(self, pcm: bytes) -> None:
        if not pcm:
            return
        self._drained.clear()
        await self._q.put(pcm)

    async def flush(self) -> None:
        """Drain queued audio, including the final partial frame."""
        self._drained.clear()
        await self._q.put(None)
        await self._drained.wait()

    def drop_pending(self) -> None:
        """Discard any buffered/queued audio immediately (for barge-in)."""
        try:
            while True:
                self._q.get_nowait()
        except asyncio.QueueEmpty:
            pass
        self._buffer.clear()
        self._drained.set()

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        period = self._frame_ms / 1000.0
        silence = b"\x00" * self._frame_bytes  # one frame of 24kHz s16le silence
        next_tick = asyncio.get_event_loop().time()
        draining = False
        try:
            while not self._stopped.is_set():
                try:
                    while True:
                        chunk = self._q.get_nowait()
                        if chunk is None:
                            draining = True
                            continue
                        self._buffer.extend(chunk)
                except asyncio.QueueEmpty:
                    pass

                if len(self._buffer) >= self._frame_bytes:
                    frame = bytes(self._buffer[: self._frame_bytes])
                    del self._buffer[: self._frame_bytes]
                elif self._buffer and draining:
                    tail = bytes(self._buffer)
                    self._buffer.clear()
                    frame = tail + (b"\x00" * (self._frame_bytes - len(tail)))
                    draining = False
                    self._drained.set()
                else:
                    if draining and not self._buffer:
                        draining = False
                        self._drained.set()
                    frame = silence
                try:
                    await self._send_fn(frame)
                except Exception:  # noqa: BLE001
                    # Don't let a transient send error kill the pacer.
                    pass

                next_tick += period
                delay = next_tick - asyncio.get_event_loop().time()
                if delay > 0:
                    await asyncio.sleep(delay)
                else:
                    # Got behind; reset to "now" so we don't burst forever.
                    next_tick = asyncio.get_event_loop().time()
        except asyncio.CancelledError:
            raise
