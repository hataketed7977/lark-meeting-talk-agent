"""Volcengine streaming ASR (BigModel / SAUC).

Protocol summary (binary frames over WebSocket):

    [4-byte header] [optional sequence] [4-byte payload size] [payload]

  Header byte 0: protocol_version (4 bits) | header_size (4 bits)
  Header byte 1: message_type (4 bits)     | message_type_flags (4 bits)
  Header byte 2: serialization (4 bits)    | compression (4 bits)
  Header byte 3: reserved (8 bits)

  message_type:
    0x01 = full-client request (the initial JSON config)
    0x02 = audio-only request
    0x09 = full-server response (JSON result)
    0x0B = server ack
    0x0F = server error

  serialization:  0x01 = JSON
  compression:    0x01 = gzip

We send: gzipped JSON for the first frame, then gzipped raw PCM for audio frames.
We receive: gzipped JSON results.

This module exposes a simple callback API:

    asr = VolcASR(on_partial=..., on_final=..., on_error=...)
    await asr.start()
    await asr.feed_pcm(pcm16k_bytes)
    await asr.stop()
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import re
import struct
import uuid
from typing import Any, Awaitable, Callable, Optional

import websockets

from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)

# Header constants.
PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001  # 4 bytes
SERIALIZATION_JSON = 0b0001
COMPRESSION_GZIP = 0b0001

# message_type
FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_REQUEST = 0b0010
FULL_SERVER_RESPONSE = 0b1001
SERVER_ACK = 0b1011
SERVER_ERROR = 0b1111

# message_type_flags
NO_SEQUENCE = 0b0000
POS_SEQUENCE = 0b0001
NEG_SEQUENCE = 0b0010
NEG_WITH_SEQUENCE = 0b0011


def _build_header(message_type: int, flags: int = NO_SEQUENCE) -> bytes:
    b0 = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    b1 = (message_type << 4) | flags
    b2 = (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP
    b3 = 0
    return bytes([b0, b1, b2, b3])


def _parse_response(data: bytes) -> dict:
    """Parse a server frame into a dict {message_type, code, payload}."""
    if len(data) < 4:
        return {}
    header_size = data[0] & 0x0F
    message_type = (data[1] >> 4) & 0x0F
    flags = data[1] & 0x0F
    compression = data[2] & 0x0F
    cursor = header_size * 4
    result: dict = {"message_type": message_type, "flags": flags}

    if message_type == FULL_SERVER_RESPONSE:
        if flags & POS_SEQUENCE:
            result["sequence"] = struct.unpack(">i", data[cursor : cursor + 4])[0]
            cursor += 4
        payload_size = struct.unpack(">I", data[cursor : cursor + 4])[0]
        cursor += 4
        payload = data[cursor : cursor + payload_size]
        if compression == COMPRESSION_GZIP and payload:
            payload = gzip.decompress(payload)
        try:
            result["payload"] = json.loads(payload.decode("utf-8")) if payload else {}
        except Exception:  # noqa: BLE001
            result["payload"] = {"_raw": payload}
    elif message_type == SERVER_ACK:
        if flags & POS_SEQUENCE:
            result["sequence"] = struct.unpack(">i", data[cursor : cursor + 4])[0]
            cursor += 4
    elif message_type == SERVER_ERROR:
        result["code"] = struct.unpack(">i", data[cursor : cursor + 4])[0]
        cursor += 4
        payload_size = struct.unpack(">I", data[cursor : cursor + 4])[0]
        cursor += 4
        payload = data[cursor : cursor + payload_size]
        if compression == COMPRESSION_GZIP and payload:
            try:
                payload = gzip.decompress(payload)
            except Exception:  # noqa: BLE001
                pass
        try:
            result["payload"] = json.loads(payload.decode("utf-8")) if payload else {}
        except Exception:  # noqa: BLE001
            result["payload"] = {"_raw": payload}
    return result


OnText = Callable[[str], Awaitable[None]]


def _is_v3_ws_url(ws_url: str) -> bool:
    return "/api/v3/" in ws_url


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


_ENGLISH_PUNCT_TRANSLATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "！": "!",
        "？": "?",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
    }
)


def _looks_english(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text)) and not bool(
        re.search(r"[\u4e00-\u9fff]", text)
    )


def _normalize_asr_text(text: str) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned or not _looks_english(cleaned):
        return cleaned
    cleaned = cleaned.translate(_ENGLISH_PUNCT_TRANSLATION)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = re.sub(r"([,.;:!?])([A-Za-z0-9])", r"\1 \2", cleaned)
    return cleaned.strip()


def _build_asr_start_request(req_id: str) -> dict[str, Any]:
    audio: dict[str, Any] = {
        "format": "pcm",
        "codec": "raw",
        "rate": CFG.asr.sample_rate,
        "bits": 16,
        "channel": 1,
    }
    if CFG.asr.language:
        audio["language"] = CFG.asr.language

    request: dict[str, Any]
    if _is_v3_ws_url(CFG.asr.ws_url):
        request = {
            "model_name": "bigmodel",
            "language": CFG.asr.language,
            "enable_itn": True,
            "enable_punc": True,
            "enable_ddc": False,
            "show_utterances": True,
            "result_type": "single",
            # Force sentence boundaries so the meeting bot receives final turns
            # during an open-ended conversation instead of only at stream end.
            "end_window_size": 800,
            "force_to_speech_time": 1000,
        }
    else:
        request = {
            "reqid": req_id,
            "nbest": 1,
            "workflow": "audio_in,resample,partition,vad,fe,decode,itn,nlu_punctuate",
            "show_language": False,
            "show_utterances": True,
            "result_type": "single",
            "sequence": 1,
        }

    return {
        "app": {
            "appid": CFG.asr.appid,
            "cluster": CFG.asr.cluster,
            "token": CFG.asr.token,
        },
        "user": {"uid": "lark_meeting_voice"},
        "audio": audio,
        "request": request,
    }


class VolcASR:
    def __init__(
        self,
        *,
        on_partial: Optional[OnText] = None,
        on_final: Optional[OnText] = None,
        on_error: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._last_partial: str = ""
        self._logged_result_shape = False
        self._feed_count = 0
        self._recv_count = 0
        self._next_sequence = 1

    async def start(self) -> None:
        headers = {
            "Authorization": f"Bearer; {CFG.asr.token}",
        }
        self._closed = asyncio.Event()
        req_id = str(uuid.uuid4())
        self._next_sequence = 1
        if "/api/v3/" in CFG.asr.ws_url:
            headers.update(
                {
                    "X-Api-App-Key": CFG.asr.appid,
                    "X-Api-Access-Key": CFG.asr.token,
                    "X-Api-Resource-Id": CFG.asr.resource_id,
                    "X-Api-Connect-Id": req_id,
                }
            )
        self._ws = await websockets.connect(
            CFG.asr.ws_url,
            additional_headers=headers,
            max_size=None,
            ping_interval=10,
            ping_timeout=10,
            open_timeout=CFG.asr.connect_timeout_s,
            close_timeout=5,
        )
        config = _build_asr_start_request(req_id)
        payload = gzip.compress(json.dumps(config).encode("utf-8"))
        if _is_v3_ws_url(CFG.asr.ws_url):
            frame = (
                _build_header(FULL_CLIENT_REQUEST, flags=POS_SEQUENCE)
                + struct.pack(">i", self._next_sequence)
                + struct.pack(">I", len(payload))
                + payload
            )
            self._next_sequence += 1
        else:
            frame = (
                _build_header(FULL_CLIENT_REQUEST)
                + struct.pack(">I", len(payload))
                + payload
            )
        async with self._send_lock:
            await asyncio.wait_for(
                self._ws.send(frame), timeout=CFG.asr.connect_timeout_s
            )
        self._recv_task = asyncio.create_task(self._recv_loop(), name="asr-recv")
        log.info(
            "Volc ASR stream started req_id=%s endpoint=%s resource_id=%s language=%s",
            req_id,
            CFG.asr.ws_url,
            CFG.asr.resource_id,
            CFG.asr.language,
        )

    async def feed_pcm(self, pcm16k: bytes) -> None:
        if not pcm16k or self._ws is None or self._closed.is_set():
            return
        self._feed_count += 1
        if self._feed_count == 1 or self._feed_count % 100 == 0:
            log.info(
                "ASR feed chunk count=%d pcm_bytes=%d",
                self._feed_count,
                len(pcm16k),
            )
        payload = gzip.compress(pcm16k)
        if _is_v3_ws_url(CFG.asr.ws_url):
            frame = (
                _build_header(AUDIO_ONLY_REQUEST, flags=POS_SEQUENCE)
                + struct.pack(">i", self._next_sequence)
                + struct.pack(">I", len(payload))
                + payload
            )
            self._next_sequence += 1
        else:
            frame = (
                _build_header(AUDIO_ONLY_REQUEST)
                + struct.pack(">I", len(payload))
                + payload
            )
        async with self._send_lock:
            try:
                await asyncio.wait_for(
                    self._ws.send(frame), timeout=CFG.asr.stream_idle_timeout_s
                )
            except (asyncio.TimeoutError, websockets.ConnectionClosed):
                self._closed.set()
                if self._on_error:
                    await self._on_error("asr_send_failed")

    async def stop(self) -> None:
        self._closed.set()
        if self._ws is not None:
            try:
                # Send final empty audio with NEG flag to signal end.
                if _is_v3_ws_url(CFG.asr.ws_url):
                    final_seq = -self._next_sequence
                    payload = b""
                    b0 = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
                    b1 = (AUDIO_ONLY_REQUEST << 4) | NEG_WITH_SEQUENCE
                    b2 = (SERIALIZATION_JSON << 4) | 0
                    b3 = 0
                    frame = (
                        bytes([b0, b1, b2, b3])
                        + struct.pack(">i", final_seq)
                        + struct.pack(">I", len(payload))
                        + payload
                    )
                else:
                    payload = gzip.compress(b"")
                    frame = (
                        _build_header(AUDIO_ONLY_REQUEST, flags=NEG_SEQUENCE)
                        + struct.pack(">I", len(payload))
                        + payload
                    )
                async with self._send_lock:
                    await self._ws.send(frame)
            except Exception:  # noqa: BLE001
                pass
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            while not self._closed.is_set():
                try:
                    msg = await asyncio.wait_for(
                        self._ws.recv(), timeout=CFG.asr.stream_idle_timeout_s
                    )
                except asyncio.TimeoutError:
                    # V3 ASR may stay quiet until speech arrives. The websocket
                    # layer already has ping/pong health checks, so lack of a
                    # server message is not itself a fatal condition.
                    log.debug(
                        "ASR recv idle for %.1fs; keeping stream open",
                        CFG.asr.stream_idle_timeout_s,
                    )
                    continue
                except websockets.ConnectionClosed:
                    break
                if not isinstance(msg, (bytes, bytearray)):
                    continue
                parsed = _parse_response(bytes(msg))
                self._recv_count += 1
                mt = parsed.get("message_type")
                if self._recv_count == 1:
                    payload = parsed.get("payload", {})
                    payload_keys = (
                        sorted(payload.keys()) if isinstance(payload, dict) else []
                    )
                    log.info(
                        "ASR first server frame message_type=%s flags=%s sequence=%s payload_keys=%s",
                        mt,
                        parsed.get("flags"),
                        parsed.get("sequence"),
                        payload_keys,
                    )
                if mt == FULL_SERVER_RESPONSE:
                    await self._handle_result(parsed)
                elif mt == SERVER_ERROR:
                    code = parsed.get("code")
                    payload = parsed.get("payload", {})
                    log.error("ASR server error code=%s payload=%s", code, payload)
                    if self._on_error:
                        await self._on_error(f"asr_error:{code}")
        except Exception as e:  # noqa: BLE001
            log.exception("ASR recv loop crashed: %s", e)
        finally:
            self._closed.set()

    async def _handle_result(self, parsed: dict) -> None:
        payload = parsed.get("payload", {})
        # Result shape is v2/v3 dependent. v3 can return partial text without
        # `utterances[].definite`, so we need a broader final-turn detector.
        results = payload.get("result") or []
        if not results:
            if payload and not self._logged_result_shape:
                self._logged_result_shape = True
                log.info(
                    "ASR payload without result: keys=%s payload=%s",
                    sorted(payload.keys()),
                    payload,
                )
            return
        if isinstance(results, dict):
            first = results
        elif isinstance(results, list):
            first = results[0]
        else:
            log.debug("Ignoring ASR result with unsupported shape: %s", payload)
            return
        text = _normalize_asr_text(first.get("text") or "")
        utterances = first.get("utterances") or []
        additions = first.get("additions") or payload.get("additions") or {}

        final_text = ""
        partial_text = text
        for u in utterances:
            if u.get("definite"):
                final_text += _normalize_asr_text(u.get("text", ""))
            else:
                # partial pieces are reflected in `text` already
                pass

        explicit_final = any(
            _as_bool(v)
            for v in (
                first.get("definite"),
                first.get("is_final"),
                first.get("final"),
                payload.get("definite"),
                payload.get("is_final"),
                payload.get("final"),
                additions.get("definite"),
                additions.get("is_final"),
                additions.get("final"),
            )
        )
        sequence = parsed.get("sequence")
        if isinstance(sequence, int) and sequence < 0:
            explicit_final = True
        if not final_text and explicit_final and text:
            final_text = text

        if final_text and final_text != self._last_partial:
            self._last_partial = ""
            if self._on_final:
                await self._on_final(final_text.strip())
        elif partial_text and partial_text != self._last_partial:
            self._last_partial = partial_text
            if self._on_partial:
                await self._on_partial(partial_text)
