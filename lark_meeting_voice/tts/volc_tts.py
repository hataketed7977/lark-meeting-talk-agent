"""Volcengine streaming TTS.

Supports three runtime modes:

- `ws_v3`: official WebSocket bidirectional streaming V3 path
- `http_v3`: TTS 2.0 HTTP V3 unidirectional streaming fallback
- `legacy_ws_v1`: older WebSocket binary protocol fallback

Public API:

    tts = VolcTTS()
    async for pcm_chunk in tts.synthesize(text, cancel_event):
        ...
    async for pcm_chunk in tts.synthesize_stream(token_stream, cancel_event):
        ...
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import struct
import uuid
from json import JSONDecodeError
from typing import Any, AsyncIterator, Dict, List, Tuple

import aiohttp
import websockets

from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)

# Binary frame constants shared by v1/v3 websocket protocols.
PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001
SERIALIZATION_RAW = 0b0000
SERIALIZATION_JSON = 0b0001
COMPRESSION_NONE = 0b0000
COMPRESSION_GZIP = 0b0001

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_RESPONSE = 0b1011
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR = 0b1111

# Official V3 control events.
START_CONNECTION_EVENT = 1
FINISH_CONNECTION_EVENT = 2
CONNECTION_STARTED_EVENT = 50
CONNECTION_FAILED_EVENT = 51
CONNECTION_FINISHED_EVENT = 52
START_SESSION_EVENT = 100
SESSION_CANCELED_EVENT = 151
FINISH_SESSION_EVENT = 102
SESSION_STARTED_EVENT = 150
SESSION_FINISHED_EVENT = 152
SESSION_FAILED_EVENT = 153
TASK_REQUEST_EVENT = 200
TTS_SENTENCE_START_EVENT = 350
TTS_SENTENCE_END_EVENT = 351
TTS_RESPONSE_EVENT = 352

# V3 request/response frames include an event number in the optional header.
WITH_EVENT = 0b0100


def _extract_response_headers(ws: Any) -> dict[str, str]:
    headers = getattr(ws, "response_headers", None)
    if headers is None:
        response = getattr(ws, "response", None)
        headers = getattr(response, "headers", None)
    if headers is None:
        return {}
    try:
        return {str(k): str(v) for k, v in headers.items()}
    except Exception:  # noqa: BLE001
        return {}


def _iter_once(text: str) -> AsyncIterator[str]:
    async def _gen() -> AsyncIterator[str]:
        yield text

    return _gen()


def _build_additions() -> str | None:
    additions: dict[str, object] = {}
    if not additions:
        return None
    return json.dumps(additions, ensure_ascii=False)


def _build_ws_v3_headers(connect_id: str) -> dict[str, str]:
    headers = {
        "X-Api-Resource-Id": CFG.tts.resource_id,
        "X-Api-Connect-Id": connect_id,
        "X-Control-Require-Usage-Tokens-Return": "*",
    }
    if CFG.tts.api_key:
        headers["X-Api-Key"] = CFG.tts.api_key
    else:
        headers["X-Api-App-Key"] = CFG.tts.appid
        headers["X-Api-Access-Key"] = CFG.tts.token
    return headers


def _build_v3_audio_params() -> dict[str, object]:
    params: dict[str, object] = {
        "format": "pcm",
        "sample_rate": CFG.tts.sample_rate,
        "speech_rate": 0,
        "loudness_rate": 0,
    }
    return params


def _build_v3_session_payload(text: str = "") -> dict[str, object]:
    req_params: dict[str, object] = {
        "text": text,
        "speaker": CFG.tts.voice_type,
        "audio_params": _build_v3_audio_params(),
    }
    additions = _build_additions()
    if additions is not None:
        req_params["additions"] = additions
    return {
        "user": {"uid": "lark_meeting_voice"},
        "event": START_SESSION_EVENT,
        "namespace": "BidirectionalTTS",
        "req_params": req_params,
    }


def _build_v3_task_payload(text: str) -> dict[str, object]:
    return {
        "user": {"uid": "lark_meeting_voice"},
        "event": TASK_REQUEST_EVENT,
        "namespace": "BidirectionalTTS",
        "req_params": {"text": text},
    }


def _build_v3_control_payload(event: int) -> dict[str, object]:
    return {
        "user": {"uid": "lark_meeting_voice"},
        "event": event,
        "namespace": "BidirectionalTTS",
    }


def _header(
    message_type: int,
    *,
    flags: int = 0,
    serialization: int = SERIALIZATION_JSON,
    compression: int = COMPRESSION_GZIP,
) -> bytes:
    b0 = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    b1 = (message_type << 4) | flags
    b2 = (serialization << 4) | compression
    return bytes([b0, b1, b2, 0])


def _json_payload(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def _sized_bytes(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _build_ws_v3_frame(
    event: int,
    payload: dict[str, object],
    *,
    session_id: str | None = None,
) -> bytes:
    body = _json_payload(payload)
    frame = _header(
        FULL_CLIENT_REQUEST,
        flags=WITH_EVENT,
        serialization=SERIALIZATION_JSON,
        compression=COMPRESSION_NONE,
    ) + struct.pack(">I", event)
    if session_id is not None:
        frame += _sized_bytes(session_id.encode("utf-8"))
    return frame + _sized_bytes(body)


def _read_sized_bytes(data: bytes, cursor: int) -> tuple[bytes, int] | None:
    if cursor + 4 > len(data):
        return None
    size = struct.unpack(">I", data[cursor : cursor + 4])[0]
    cursor += 4
    if size < 0 or cursor + size > len(data):
        return None
    return data[cursor : cursor + size], cursor + size


def _decode_ws_v3_payload(
    payload: bytes, serialization: int, compression: int
) -> object:
    if compression == COMPRESSION_GZIP and payload:
        payload = gzip.decompress(payload)
    if serialization == SERIALIZATION_JSON and payload:
        try:
            return json.loads(payload.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {"_raw": payload}
    return payload


class VolcTTS:
    def __init__(self) -> None:
        self._send_lock = asyncio.Lock()

    async def synthesize(
        self,
        text: str,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        if not text.strip() or cancel_event.is_set():
            return
        if CFG.tts.mode == "ws_v3":
            async for chunk in self._synthesize_ws_v3(_iter_once(text), cancel_event):
                yield chunk
            return
        if CFG.tts.mode == "http_v3" or CFG.tts.api_version.startswith("2"):
            async for chunk in self._synthesize_http_v3(text, cancel_event):
                yield chunk
            return

        async for chunk in self._synthesize_ws_v1(text, cancel_event):
            yield chunk

    async def synthesize_stream(
        self,
        text_stream: AsyncIterator[str],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        if CFG.tts.mode == "ws_v3":
            async for chunk in self._synthesize_ws_v3(text_stream, cancel_event):
                yield chunk
            return

        from lark_meeting_voice.llm.openai_compatible import sentence_chunks

        async for sentence in sentence_chunks(
            text_stream,
            cancel_event,
            min_chars=CFG.llm.tts_chunk_min_chars,
            max_chars=CFG.llm.tts_chunk_max_chars,
        ):
            async for chunk in self.synthesize(sentence, cancel_event):
                yield chunk

    async def _send_ws_v3_event(
        self,
        ws: Any,
        event: int,
        payload: dict[str, object],
        *,
        session_id: str | None = None,
    ) -> None:
        frame = _build_ws_v3_frame(event, payload, session_id=session_id)
        async with self._send_lock:
            await asyncio.wait_for(ws.send(frame), timeout=CFG.tts.connect_timeout_s)

    async def _await_ws_v3_event(
        self,
        ws: Any,
        expected_event: int,
        cancel_event: asyncio.Event,
    ) -> str:
        while True:
            if cancel_event.is_set():
                raise asyncio.CancelledError()
            try:
                msg = await asyncio.wait_for(
                    ws.recv(), timeout=CFG.tts.stream_idle_timeout_s
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError(
                    f"TTS WS V3 timed out waiting for event={expected_event}"
                ) from exc
            parsed = _parse_ws_v3_message(msg)
            if parsed["kind"] == "error":
                raise RuntimeError(
                    f"TTS WS V3 error code={parsed.get('code')} payload={parsed.get('payload')}"
                )
            if parsed["kind"] == "control" and parsed.get("event") == expected_event:
                return str(
                    parsed.get("connection_id") or parsed.get("session_id") or ""
                )
            if parsed["kind"] == "control" and parsed.get("event") in {
                CONNECTION_FAILED_EVENT,
                SESSION_FAILED_EVENT,
            }:
                raise RuntimeError(
                    f"TTS WS V3 failed event={parsed.get('event')} payload={parsed.get('payload')}"
                )

    async def _send_ws_v3_text(
        self,
        ws: Any,
        text_stream: AsyncIterator[str],
        cancel_event: asyncio.Event,
        session_id: str,
    ) -> None:
        async for text in text_stream:
            if cancel_event.is_set():
                break
            if not text:
                continue
            await self._send_ws_v3_event(
                ws,
                TASK_REQUEST_EVENT,
                _build_v3_task_payload(text),
                session_id=session_id,
            )
        await self._send_ws_v3_event(
            ws,
            FINISH_SESSION_EVENT,
            _build_v3_control_payload(FINISH_SESSION_EVENT),
            session_id=session_id,
        )

    async def _synthesize_ws_v3(
        self,
        text_stream: AsyncIterator[str],
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        connect_id = str(uuid.uuid4())
        session_id = uuid.uuid4().hex
        headers = _build_ws_v3_headers(connect_id)
        ws = await websockets.connect(
            CFG.tts.ws_url,
            additional_headers=headers,
            max_size=None,
            ping_interval=10,
            ping_timeout=10,
            open_timeout=CFG.tts.connect_timeout_s,
            close_timeout=5,
        )
        response_headers = _extract_response_headers(ws)
        log.info(
            "Volc TTS WS V3 connected connect_id=%s endpoint=%s resource_id=%s voice_type=%s logid=%s",
            connect_id,
            CFG.tts.ws_url,
            CFG.tts.resource_id,
            CFG.tts.voice_type,
            response_headers.get("X-Tt-Logid", ""),
        )
        sender_task: asyncio.Task[None] | None = None
        try:
            await self._send_ws_v3_event(
                ws,
                START_CONNECTION_EVENT,
                _build_v3_control_payload(START_CONNECTION_EVENT),
            )
            server_connect_id = await self._await_ws_v3_event(
                ws, CONNECTION_STARTED_EVENT, cancel_event
            )
            if server_connect_id:
                log.debug("TTS WS V3 server connection_id=%s", server_connect_id)
            await self._send_ws_v3_event(
                ws,
                START_SESSION_EVENT,
                _build_v3_session_payload(),
                session_id=session_id,
            )
            await self._await_ws_v3_event(ws, SESSION_STARTED_EVENT, cancel_event)
            sender_task = asyncio.create_task(
                self._send_ws_v3_text(ws, text_stream, cancel_event, session_id),
                name="tts-ws-v3-send",
            )
            while True:
                if cancel_event.is_set() and sender_task.done():
                    break
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=CFG.tts.stream_idle_timeout_s
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "TTS WS V3 stream idle timeout after %.1fs",
                        CFG.tts.stream_idle_timeout_s,
                    )
                    break
                except websockets.ConnectionClosed:
                    break
                parsed = _parse_ws_v3_message(msg)
                if parsed["kind"] == "audio":
                    chunk = parsed.get("audio", b"")
                    if isinstance(chunk, bytes) and chunk:
                        yield chunk
                    continue
                if parsed["kind"] == "error":
                    log.error(
                        "TTS WS V3 error code=%s payload=%s",
                        parsed.get("code"),
                        parsed.get("payload"),
                    )
                    break
                if (
                    parsed["kind"] == "control"
                    and parsed.get("event") == SESSION_FINISHED_EVENT
                ):
                    break
        finally:
            if sender_task is not None:
                try:
                    await sender_task
                except Exception as e:  # noqa: BLE001
                    log.warning("TTS WS V3 sender task failed: %s", e)
            try:
                await self._send_ws_v3_event(
                    ws,
                    FINISH_CONNECTION_EVENT,
                    _build_v3_control_payload(FINISH_CONNECTION_EVENT),
                )
            except Exception as e:  # noqa: BLE001
                log.debug("TTS WS V3 finish connection failed: %s", e)
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass

    async def _synthesize_http_v3(
        self,
        text: str,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        req_id = str(uuid.uuid4())
        payload = {
            "user": {"uid": "lark_meeting_voice"},
            "namespace": "BidirectionalTTS",
            "req_params": {
                "text": text,
                "speaker": CFG.tts.voice_type,
                "audio_params": {
                    "format": "pcm",
                    "sample_rate": CFG.tts.sample_rate,
                    "speech_rate": 0,
                    "loudness_rate": 0,
                    "pitch": 0,
                },
            },
        }
        headers = {
            "Content-Type": "application/json",
            "X-Api-App-Id": CFG.tts.appid,
            "X-Api-Access-Key": CFG.tts.token,
            "X-Api-Resource-Id": CFG.tts.resource_id,
            "X-Api-Request-Id": req_id,
        }
        log.info(
            "Volc TTS 2.0 stream starting req_id=%s resource_id=%s voice_type=%s",
            req_id,
            CFG.tts.resource_id,
            CFG.tts.voice_type,
        )
        timeout = aiohttp.ClientTimeout(
            total=None,
            connect=CFG.tts.connect_timeout_s,
            sock_read=CFG.tts.stream_idle_timeout_s,
        )
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                CFG.tts.http_url,
                headers=headers,
                json=payload,
            ) as resp:
                logid = resp.headers.get("X-Tt-Logid", "")
                if resp.status >= 400:
                    body = await resp.text()
                    log.error(
                        "TTS 2.0 HTTP error status=%s logid=%s body=%s",
                        resp.status,
                        logid,
                        body[:500],
                    )
                    return

                buf = ""
                async for raw in resp.content.iter_any():
                    if cancel_event.is_set():
                        log.info("TTS cancelled mid-stream")
                        break
                    if not raw:
                        continue
                    buf += raw.decode("utf-8", errors="ignore")
                    parsed, buf = _pop_stream_json_objects(buf)
                    for item in parsed:
                        chunk, done = _decode_http_v3_audio(item)
                        if chunk:
                            yield chunk
                        if done:
                            return
                for item in _pop_stream_json_objects(buf, final=True)[0]:
                    chunk, done = _decode_http_v3_audio(item)
                    if chunk:
                        yield chunk
                    if done:
                        return

    async def _synthesize_ws_v1(
        self,
        text: str,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        if not text.strip() or cancel_event.is_set():
            return

        req_id = str(uuid.uuid4())
        request = {
            "app": {
                "appid": CFG.tts.appid,
                "token": CFG.tts.token,
                "cluster": CFG.tts.cluster,
            },
            "user": {"uid": "lark_meeting_voice"},
            "audio": {
                "voice_type": CFG.tts.voice_type,
                "encoding": "pcm",
                "rate": CFG.tts.sample_rate,
                "bits": 16,
                "channel": 1,
                "speed_ratio": 1.0,
                "volume_ratio": 1.0,
                "pitch_ratio": 1.0,
            },
            "request": {
                "reqid": req_id,
                "text": text,
                "text_type": "plain",
                "operation": "submit",
            },
        }
        payload = gzip.compress(json.dumps(request).encode("utf-8"))
        frame = _header(FULL_CLIENT_REQUEST) + struct.pack(">I", len(payload)) + payload

        headers = {
            "Authorization": f"Bearer; {CFG.tts.token}",
        }

        ws = await websockets.connect(
            CFG.tts.ws_url,
            additional_headers=headers,
            max_size=None,
            ping_interval=10,
            ping_timeout=10,
            open_timeout=CFG.tts.connect_timeout_s,
            close_timeout=5,
        )
        try:
            await asyncio.wait_for(ws.send(frame), timeout=CFG.tts.connect_timeout_s)
            while True:
                if cancel_event.is_set():
                    log.info("TTS cancelled mid-stream")
                    break
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=CFG.tts.stream_idle_timeout_s
                    )
                except asyncio.TimeoutError:
                    log.warning(
                        "TTS stream idle timeout after %.1fs",
                        CFG.tts.stream_idle_timeout_s,
                    )
                    break
                except websockets.ConnectionClosed:
                    break
                if not isinstance(msg, (bytes, bytearray)):
                    continue
                data = bytes(msg)
                if len(data) < 4:
                    continue
                header_size = data[0] & 0x0F
                message_type = (data[1] >> 4) & 0x0F
                flags = data[1] & 0x0F
                cursor = header_size * 4

                if message_type == AUDIO_ONLY_RESPONSE:
                    # Optional 4-byte sequence
                    if flags == 0b0001 or flags == 0b0011:
                        seq = struct.unpack(">i", data[cursor : cursor + 4])[0]
                        cursor += 4
                    else:
                        seq = 0
                    if cursor + 4 > len(data):
                        continue
                    audio_size = struct.unpack(">I", data[cursor : cursor + 4])[0]
                    cursor += 4
                    chunk = data[cursor : cursor + audio_size]
                    if chunk:
                        yield chunk
                    # Negative sequence = last chunk.
                    if seq < 0:
                        break
                elif message_type == FULL_SERVER_RESPONSE:
                    # final ack with no audio
                    break
                elif message_type == SERVER_ERROR:
                    code = struct.unpack(">i", data[cursor : cursor + 4])[0]
                    cursor += 4
                    err_size = struct.unpack(">I", data[cursor : cursor + 4])[0]
                    cursor += 4
                    err_payload = data[cursor : cursor + err_size]
                    try:
                        err_payload = gzip.decompress(err_payload)
                    except Exception:  # noqa: BLE001
                        pass
                    log.error("TTS error code=%s payload=%s", code, err_payload)
                    break
        finally:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass


def _pop_stream_json_objects(
    buf: str,
    *,
    final: bool = False,
) -> Tuple[List[Dict[str, object]], str]:
    """Parse newline/SSE/concatenated JSON objects from the TTS 2.0 stream."""
    items: List[Dict[str, object]] = []
    decoder = json.JSONDecoder()
    pos = 0
    length = len(buf)
    while pos < length:
        while pos < length and buf[pos].isspace():
            pos += 1
        if pos >= length:
            break
        if buf.startswith("data:", pos):
            pos += len("data:")
            while pos < length and buf[pos].isspace():
                pos += 1
        try:
            obj, end = decoder.raw_decode(buf, pos)
        except JSONDecodeError:
            if final:
                log.warning(
                    "Ignoring trailing incomplete TTS stream payload: %r", buf[pos:]
                )
                return items, ""
            return items, buf[pos:]
        if isinstance(obj, dict):
            items.append(obj)
        pos = end
    return items, ""


def _decode_http_v3_audio(item: Dict[str, object]) -> Tuple[bytes, bool]:
    code = item.get("code")
    data = item.get("data")
    success_codes = {None, 0, "0", 20000000, "20000000"}
    if code not in success_codes:
        log.error("TTS 2.0 stream error: %s", item)
        return b"", True

    chunk = b""
    if isinstance(data, str) and data:
        try:
            chunk = base64.b64decode(data)
        except Exception as e:  # noqa: BLE001
            log.error("Failed to decode TTS 2.0 audio chunk: %s", e)

    sequence = item.get("sequence")
    done = isinstance(sequence, int) and sequence < 0
    if isinstance(sequence, str):
        try:
            done = int(sequence) < 0
        except ValueError:
            done = False
    # HTTP V3 may emit a final success status object with no audio payload and
    # no negative sequence. Treat it as a clean end-of-stream, not an error.
    if not done and data in (None, "") and code in {20000000, "20000000"}:
        done = True
    return chunk, done


def _parse_ws_v3_message(msg: object) -> dict[str, object]:
    if isinstance(msg, str):
        return {"kind": "text", "payload": msg}
    if not isinstance(msg, (bytes, bytearray)):
        return {"kind": "unknown"}
    data = bytes(msg)
    if len(data) < 4:
        return {"kind": "unknown"}

    header_size = data[0] & 0x0F
    message_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    serialization = (data[2] >> 4) & 0x0F
    compression = data[2] & 0x0F
    cursor = header_size * 4
    event = None
    if flags == WITH_EVENT and cursor + 4 <= len(data):
        event = struct.unpack(">I", data[cursor : cursor + 4])[0]
        cursor += 4

    if message_type == SERVER_ERROR:
        if cursor + 4 > len(data):
            return {"kind": "error", "event": event}
        code = struct.unpack(">i", data[cursor : cursor + 4])[0]
        cursor += 4
        if cursor + 4 > len(data):
            return {"kind": "error", "event": event, "code": code}
        payload_size = struct.unpack(">I", data[cursor : cursor + 4])[0]
        cursor += 4
        payload = data[cursor : cursor + payload_size]
        decoded = _decode_ws_v3_payload(payload, serialization, compression)
        return {"kind": "error", "event": event, "code": code, "payload": decoded}

    connection_id = ""
    session_id = ""
    if event in {
        CONNECTION_STARTED_EVENT,
        CONNECTION_FINISHED_EVENT,
        CONNECTION_FAILED_EVENT,
    }:
        field = _read_sized_bytes(data, cursor)
        if field is not None:
            raw_connection_id, cursor = field
            connection_id = raw_connection_id.decode("utf-8", errors="replace")
    elif event in {
        SESSION_STARTED_EVENT,
        SESSION_CANCELED_EVENT,
        SESSION_FINISHED_EVENT,
        SESSION_FAILED_EVENT,
        TTS_SENTENCE_START_EVENT,
        TTS_SENTENCE_END_EVENT,
        TTS_RESPONSE_EVENT,
    }:
        field = _read_sized_bytes(data, cursor)
        if field is not None:
            raw_session_id, cursor = field
            session_id = raw_session_id.decode("utf-8", errors="replace")

    field = _read_sized_bytes(data, cursor)
    if field is None:
        return {
            "kind": "unknown",
            "event": event,
            "connection_id": connection_id,
            "session_id": session_id,
        }
    payload, _ = field

    if message_type == AUDIO_ONLY_RESPONSE:
        return {
            "kind": "audio",
            "event": event,
            "session_id": session_id,
            "audio": payload,
        }

    decoded_payload = _decode_ws_v3_payload(payload, serialization, compression)
    return {
        "kind": "control" if message_type == FULL_SERVER_RESPONSE else "unknown",
        "event": event,
        "connection_id": connection_id,
        "session_id": session_id,
        "payload": decoded_payload,
    }
