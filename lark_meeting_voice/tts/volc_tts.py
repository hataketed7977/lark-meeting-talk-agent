"""Volcengine streaming TTS (BigTTS / bigmodel).

Supports the legacy WebSocket binary protocol and the TTS 2.0 HTTP V3
unidirectional streaming API. We request `audio/pcm` at 24 kHz s16le mono so
the output matches Feishu's upstream format byte-for-byte (no resampling
needed).

Public API:

    tts = VolcTTS()
    async for pcm_chunk in tts.synthesize(text, cancel_event):
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
from typing import AsyncIterator, Dict, Iterator, List, Tuple

import aiohttp
import websockets

from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)

# Header is identical layout to ASR.
PROTOCOL_VERSION = 0b0001
DEFAULT_HEADER_SIZE = 0b0001
SERIALIZATION_JSON = 0b0001
COMPRESSION_GZIP = 0b0001

FULL_CLIENT_REQUEST = 0b0001
AUDIO_ONLY_RESPONSE = 0b1011
FULL_SERVER_RESPONSE = 0b1001
SERVER_ERROR = 0b1111


def _header(message_type: int) -> bytes:
    b0 = (PROTOCOL_VERSION << 4) | DEFAULT_HEADER_SIZE
    b1 = (message_type << 4) | 0
    b2 = (SERIALIZATION_JSON << 4) | COMPRESSION_GZIP
    return bytes([b0, b1, b2, 0])


class VolcTTS:
    def __init__(self) -> None:
        pass

    async def synthesize(
        self,
        text: str,
        cancel_event: asyncio.Event,
    ) -> AsyncIterator[bytes]:
        if not text.strip() or cancel_event.is_set():
            return
        if CFG.tts.api_version.startswith("2"):
            async for chunk in self._synthesize_http_v3(text, cancel_event):
                yield chunk
            return

        async for chunk in self._synthesize_ws_v1(text, cancel_event):
            yield chunk

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
