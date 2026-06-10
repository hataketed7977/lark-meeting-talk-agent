"""ASR backend interface."""

from __future__ import annotations

from typing import Awaitable, Callable, Protocol

OnText = Callable[[str], Awaitable[None]]
OnError = Callable[[str], Awaitable[None]]


class SpeechRecognizer(Protocol):
    async def start(self) -> None: ...

    async def feed_pcm(self, pcm16k: bytes) -> None: ...

    async def stop(self) -> None: ...
