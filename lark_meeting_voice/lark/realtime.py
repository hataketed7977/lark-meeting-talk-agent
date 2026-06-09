"""Feishu Realtime WebSocket client.

Handles the three-layer protocol:

    WebSocket binary  ->  Frontier Frame (proto2)
                            ->  meeting_realtime.v1.ClientEvent/ServerEvent (proto3)
                              ->  raw PCM bytes

Public API:

    rt = RealtimeClient(ws_url)
    await rt.connect()                 # opens WS and sends session.create
    await rt.wait_session_created()    # blocks until session is ready
    async for evt in rt.downstream():  # AudioDownstreamDelta events
        ...
    await rt.send_audio(pcm_bytes)
    await rt.send_clear()
    await rt.close(reason="USER_LEFT")
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import websockets

from lark_meeting_voice._pb import frontier_pb2 as fr
from lark_meeting_voice._pb import meeting_realtime_pb2 as mr

log = logging.getLogger(__name__)

# Frontier constants from the official doc.
FRONTIER_SERVICE = 33555721
FRONTIER_METHOD = 1
FRONTIER_PAYLOAD_ENCODING = "binary"
FRONTIER_PAYLOAD_TYPE = "application/x-protobuf"
FRONTIER_FRAME_TYPE_NORMAL = 0

# Frame types we should silently skip on the recv side.
# NOTE: frame_type=1 (NeedAck) is NOT in here — we must reply with an IsAck
# (frame_type=2) or the server kicks us with session.closed reason=0 after
# about a second.
FRONTIER_FRAME_TYPE_NEED_ACK = 1
FRONTIER_FRAME_TYPE_IS_ACK = 2
FRONTIER_CTRL_FRAME_TYPES = {2, 16, 32}  # IsAck, CursorKeyFrame, CursorDataFrame

# Audio format (24 kHz s16le mono) — fixed by the meeting endpoint.
SAMPLE_RATE = 24000
AUDIO_TYPE = "audio/pcm"
AUDIO_ENCODING = "s16le"


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _new_event_id() -> str:
    return str(uuid.uuid4())


def _audio_format() -> mr.AudioFormat:
    return mr.AudioFormat(type=AUDIO_TYPE, encoding=AUDIO_ENCODING, sample_rate=SAMPLE_RATE)


def _wrap_in_frame(client_event_bytes: bytes, msg_id: str) -> bytes:
    frame = fr.Frame(
        SeqID=0,
        LogID=0,
        service=FRONTIER_SERVICE,
        method=FRONTIER_METHOD,
        payload_encoding=FRONTIER_PAYLOAD_ENCODING,
        payload_type=FRONTIER_PAYLOAD_TYPE,
        payload=client_event_bytes,
        msg_id=msg_id,
        frame_type=FRONTIER_FRAME_TYPE_NORMAL,
    )
    return frame.SerializeToString()


@dataclass
class DownstreamAudio:
    track_id: str
    source: str
    pts_ms: int
    duration_ms: int
    pcm: bytes


class RealtimeClient:
    def __init__(self, ws_url: str) -> None:
        self._ws_url = ws_url
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._session_id: int = 0
        self._created_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._downstream_q: asyncio.Queue[DownstreamAudio] = asyncio.Queue(maxsize=512)
        self._closed = asyncio.Event()
        self._recv_task: Optional[asyncio.Task] = None
        # Diagnostics
        self._tx_audio_count = 0
        self._rx_audio_count = 0
        self._connect_time: float = 0.0
        self._session_created_time: float = 0.0
        # Rate-limit repetitive server errors (e.g. stale-publish 1001 flood).
        self._err_seen: dict[tuple[int, str], int] = {}

    @property
    def session_id(self) -> int:
        return self._session_id

    async def connect(self) -> None:
        log.info("Connecting Realtime WS %s", self._ws_url)
        self._connect_time = time.monotonic()
        self._ws = await websockets.connect(
            self._ws_url,
            max_size=None,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
        )
        log.info("WS connected (t+%.3fs)", time.monotonic() - self._connect_time)
        self._recv_task = asyncio.create_task(self._recv_loop(), name="realtime-recv")
        await self._send_session_create()

    async def wait_session_created(self, timeout: float = 10.0) -> None:
        try:
            await asyncio.wait_for(self._created_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("Timed out waiting for session.created")

    async def _send_session_create(self) -> None:
        ev = mr.ClientEvent(
            type="session.create",
            event_id=_new_event_id(),
            session_id=0,
            created_at=_now_rfc3339(),
            session_create=mr.SessionCreate(
                session=mr.Session(media=mr.Media(
                    audio_upstream_format=_audio_format(),
                    audio_downstream_format=_audio_format(),
                )),
            ),
        )
        await self._send_client_event(ev)

    async def _send_client_event(self, ev: mr.ClientEvent) -> None:
        if self._ws is None:
            raise RuntimeError("WS not connected")
        client_bytes = ev.SerializeToString()
        frame_bytes = _wrap_in_frame(client_bytes, ev.event_id)
        async with self._send_lock:
            await self._ws.send(frame_bytes)
        # Audio frames are too frequent to log per-event; just count them.
        if ev.type == "audio.upstream.append":
            self._tx_audio_count += 1
        else:
            log.info("TX %s event_id=%s session_id=%s",
                     ev.type, ev.event_id, ev.session_id)

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            return
        ev = mr.ClientEvent(
            type="audio.upstream.append",
            event_id=_new_event_id(),
            session_id=self._session_id,
            created_at=_now_rfc3339(),
            audio_upstream_append=mr.AudioUpstreamAppend(delta=pcm),
        )
        await self._send_client_event(ev)

    async def send_clear(self) -> None:
        ev = mr.ClientEvent(
            type="audio.upstream.clear",
            event_id=_new_event_id(),
            session_id=self._session_id,
            created_at=_now_rfc3339(),
            audio_upstream_clear=mr.AudioUpstreamClear(),
        )
        await self._send_client_event(ev)
        log.info("Sent audio.upstream.clear (session=%s)", self._session_id)

    async def close(self, reason: str = "USER_LEFT") -> None:
        if self._ws is None or self._closed.is_set():
            return
        try:
            ev = mr.ClientEvent(
                type="session.close",
                event_id=_new_event_id(),
                session_id=self._session_id,
                created_at=_now_rfc3339(),
                session_close=mr.SessionClose(
                    reason=mr.USER_LEFT if reason == "USER_LEFT" else mr.CLIENT_ERROR,
                ),
            )
            await self._send_client_event(ev)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send session.close: %s", e)
        self._closed.set()
        try:
            await self._ws.close()
        except Exception:  # noqa: BLE001
            pass
        if self._recv_task:
            self._recv_task.cancel()

    async def downstream(self) -> AsyncIterator[DownstreamAudio]:
        while not self._closed.is_set():
            try:
                item = await asyncio.wait_for(self._downstream_q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield item

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for msg in self._ws:
                if not isinstance(msg, (bytes, bytearray)):
                    log.warning("Ignoring non-binary message type=%s len=%s",
                                type(msg).__name__, len(msg) if hasattr(msg, "__len__") else "?")
                    continue
                self._handle_binary(bytes(msg))
        except websockets.ConnectionClosed as e:
            log.warning(
                "WS closed: code=%s reason=%r rcvd_then_sent=%s "
                "(tx_audio=%d rx_audio=%d)",
                e.code, e.reason, getattr(e, "rcvd_then_sent", None),
                self._tx_audio_count, self._rx_audio_count,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("recv loop error: %s", e)
        finally:
            self._closed.set()

    def _handle_binary(self, data: bytes) -> None:
        try:
            frame = fr.Frame()
            frame.ParseFromString(data)
        except Exception as e:  # noqa: BLE001
            log.warning("Bad Frontier Frame: %s", e)
            return
        ft = frame.frame_type if frame.HasField("frame_type") else 0

        # NeedAck: server expects us to echo back an IsAck immediately,
        # otherwise it kicks us after ~1s with session.closed reason=0.
        if ft == FRONTIER_FRAME_TYPE_NEED_ACK:
            log.debug("Frontier NeedAck SeqID=%s -> sending IsAck", frame.SeqID)
            asyncio.create_task(self._send_ack(frame))
            return

        if ft in FRONTIER_CTRL_FRAME_TYPES:
            log.debug("Skip Frontier ctrl frame_type=%s", ft)
            return
        if not frame.HasField("payload") or not frame.payload:
            log.debug("Frontier frame has no payload, skipping")
            return
        try:
            ev = mr.ServerEvent()
            ev.ParseFromString(frame.payload)
        except Exception as e:  # noqa: BLE001
            log.warning("Bad ServerEvent payload: %s (first 32 bytes hex=%s)",
                        e, frame.payload[:32].hex())
            return
        self._dispatch(ev, frame)

    async def _send_ack(self, src: "fr.Frame") -> None:
        """Reply to a NeedAck frame with an IsAck frame."""
        if self._ws is None:
            return
        ack = fr.Frame(
            SeqID=src.SeqID,
            LogID=src.LogID,
            service=FRONTIER_SERVICE,
            method=FRONTIER_METHOD,
            payload_encoding=FRONTIER_PAYLOAD_ENCODING,
            payload_type=FRONTIER_PAYLOAD_TYPE,
            msg_id=src.msg_id,
            frame_type=FRONTIER_FRAME_TYPE_IS_ACK,
        )
        try:
            async with self._send_lock:
                await self._ws.send(ack.SerializeToString())
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to send IsAck: %s", e)

    def _dispatch(self, ev: mr.ServerEvent, frame: "fr.Frame | None" = None) -> None:
        t = ev.type
        if t == "session.created":
            self._session_id = ev.session_id
            self._session_created_time = time.monotonic()
            elapsed_since_connect = (
                self._session_created_time - self._connect_time
            ) if self._connect_time else 0.0
            self._created_event.set()
            log.info(
                "session.created session_id=%s client_event_id=%s "
                "(t+%.3fs after connect)",
                self._session_id, ev.session_created.client_event_id,
                elapsed_since_connect,
            )
        elif t == "audio.downstream.delta":
            d = ev.audio_downstream_delta
            self._rx_audio_count += 1
            # Only log the first frame per session to confirm downstream works.
            if self._rx_audio_count == 1:
                log.info("First downstream audio frame received: track=%s source=%s "
                         "format=%s/%s/%sHz",
                         d.track_id, d.source,
                         d.format.type, d.format.encoding, d.format.sample_rate)
            try:
                self._downstream_q.put_nowait(DownstreamAudio(
                    track_id=d.track_id,
                    source=d.source,
                    pts_ms=int(d.pts_ms),
                    duration_ms=int(d.duration_ms),
                    pcm=d.delta,
                ))
            except asyncio.QueueFull:
                log.warning("downstream queue full, dropping frame")
        elif t == "session.closed":
            reason_val = ev.session_closed.reason
            reason_name = {
                0: "UNSPECIFIED",
                1: "SERVER_SHUTDOWN",
                2: "IDLE_TIMEOUT",
                3: "INTERNAL_ERROR",
            }.get(reason_val, f"UNKNOWN({reason_val})")
            elapsed_since_created = (
                time.monotonic() - self._session_created_time
            ) if self._session_created_time else 0.0
            log_id = ""
            log_id_new = ""
            if frame is not None:
                log_id = str(frame.LogID)
                log_id_new = (
                    frame.LogIDNew if frame.HasField("LogIDNew") else ""
                )
            log.warning(
                "session.closed reason=%s (%d) session_id=%s "
                "(t+%.3fs after session.created, tx_audio=%d, rx_audio=%d) "
                "[Frontier LogID=%s LogIDNew=%s msg_id=%s] "
                "— server kicked us; common causes: identity mismatch "
                "between bots/join and realtime/endpoint, bot not actually "
                "in meeting, duplicate session for same bot, or another "
                "process holding a realtime session for the same identity.",
                reason_name, reason_val, ev.session_closed.session_id,
                elapsed_since_created, self._tx_audio_count, self._rx_audio_count,
                log_id, log_id_new,
                frame.msg_id if (frame is not None and frame.HasField("msg_id")) else "",
            )
            self._closed.set()
        elif t == "error":
            err = ev.error
            key = (err.code, err.message)
            count = self._err_seen.get(key, 0) + 1
            self._err_seen[key] = count
            # First occurrence: log full detail. After that, only log every
            # 100th repeat to avoid flooding.
            if count == 1:
                log.error(
                    "Server error: code=%s msg=%s retryable=%s details=%s "
                    "(triggered by client_event_id=%s)",
                    err.code, err.message, err.retryable, dict(err.details),
                    err.client_event_id,
                )
            elif count % 100 == 0:
                log.error("Server error code=%s repeated %d times", err.code, count)
            # Highlight the "another process owns the publisher slot" case —
            # this means our audio.upstream.append frames are being silently
            # dropped and the bot will be inaudible in the meeting.
            if count == 1 and err.code == 1001 and "stale stream publish session" in err.message:
                log.critical(
                    "UPSTREAM PUBLISHER SLOT CONFLICT: another realtime session "
                    "already owns the publisher for this bot identity "
                    "(current=%s, incoming=%s). Our TTS audio will NOT reach the "
                    "meeting. Likely causes: (a) another realtime client holds a realtime "
                    "session for the same bot, (b) a previous voice subprocess "
                    "didn't release cleanly. Resolution: ensure only one realtime "
                    "session exists per bot identity, or use a separate bot.",
                    err.details.get("current", "?"),
                    err.details.get("incoming", self._session_id),
                )
        else:
            log.warning(
                "Unhandled ServerEvent type=%r event_id=%s session_id=%s "
                "(this is unusual — log so we can extend the dispatcher)",
                t, ev.event_id, ev.session_id,
            )
