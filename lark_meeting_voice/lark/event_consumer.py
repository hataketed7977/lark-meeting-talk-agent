from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import dataclass
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

CURRENT_MEETING_EVENT_KEYS = (
    "vc.meeting.participant_meeting_ended_v1",
    "vc.note.generated_v1",
    "minutes.minute.generated_v1",
)


def build_meeting_event_jq(event_key: str, meeting_id: str) -> str:
    quoted = json.dumps(meeting_id)
    if event_key == "vc.meeting.participant_meeting_ended_v1":
        return f"select(.meeting_id == {quoted})"
    if event_key == "vc.note.generated_v1":
        return (
            "select(.note_source.source_type == \"meeting\" and "
            f".note_source.source_entity_id == {quoted})"
        )
    if event_key == "minutes.minute.generated_v1":
        return (
            "select(.minute_source.source_type == \"meeting\" and "
            f".minute_source.source_entity_id == {quoted})"
        )
    raise ValueError(f"Unsupported meeting event key: {event_key}")


@dataclass(frozen=True)
class MeetingEvent:
    event_key: str
    payload: dict


class _EventConsumerProcess:
    def __init__(
        self,
        event_key: str,
        meeting_id: str,
        on_event: Callable[[MeetingEvent], Awaitable[None]],
    ) -> None:
        self._event_key = event_key
        self._meeting_id = meeting_id
        self._on_event = on_event
        self._proc: asyncio.subprocess.Process | None = None
        self._stdout_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        jq = build_meeting_event_jq(self._event_key, self._meeting_id)
        command = (
            "tail -f /dev/null | "
            "lark-cli event consume "
            f"{shlex.quote(self._event_key)} "
            "--as user "
            f"--jq {shlex.quote(jq)}"
        )
        self._proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stdout_task = asyncio.create_task(
            self._read_stdout(), name=f"event-stdout-{self._event_key}"
        )
        self._stderr_task = asyncio.create_task(
            self._read_stderr(), name=f"event-stderr-{self._event_key}"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("Event consumer ready timeout key=%s", self._event_key)

    async def stop(self) -> None:
        if self._proc is None:
            return
        try:
            self._proc.terminate()
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            log.warning("Event consumer terminate timeout key=%s", self._event_key)
            self._proc.kill()
            await self._proc.wait()
        for task in (self._stdout_task, self._stderr_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        while True:
            line = await self._proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                log.warning("Event consumer bad JSON key=%s line=%r", self._event_key, text)
                continue
            await self._on_event(MeetingEvent(event_key=self._event_key, payload=payload))

    async def _read_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        ready_marker = f"[event] ready event_key={self._event_key}"
        while True:
            line = await self._proc.stderr.readline()
            if not line:
                if not self._ready.is_set():
                    self._ready.set()
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if ready_marker in text:
                self._ready.set()
                log.info("Meeting event consumer ready key=%s", self._event_key)
                continue
            if text:
                log.info("Meeting event consumer key=%s :: %s", self._event_key, text)


class CurrentMeetingEventConsumers:
    def __init__(
        self,
        meeting_id: str,
        on_event: Callable[[MeetingEvent], Awaitable[None]],
    ) -> None:
        self._meeting_id = meeting_id
        self._on_event = on_event
        self._workers = [
            _EventConsumerProcess(event_key, meeting_id, on_event)
            for event_key in CURRENT_MEETING_EVENT_KEYS
        ]

    async def start(self) -> None:
        for worker in self._workers:
            try:
                await worker.start()
            except FileNotFoundError:
                log.warning("lark-cli not found; meeting event consumers disabled")
                return
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "Failed to start meeting event consumer key=%s: %s",
                    worker._event_key,  # noqa: SLF001
                    exc,
                )

    async def stop(self) -> None:
        for worker in self._workers:
            await worker.stop()
