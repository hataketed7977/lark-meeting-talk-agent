"""Volcengine streaming TTS (BigTTS / bigmodel).

Uses the public binary streaming protocol. Same header layout as the ASR API.
We request `audio/pcm` at 24 kHz s16le mono so the output matches Feishu's
upstream format byte-for-byte (no resampling needed).

Public API:

    tts = VolcTTS()
    async for pcm_chunk in tts.synthesize(text, cancel_event):
        ...
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import struct
import uuid
from typing import AsyncIterator

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
                    msg = await asyncio.wait_for(ws.recv(), timeout=CFG.tts.stream_idle_timeout_s)
                except asyncio.TimeoutError:
                    log.warning("TTS stream idle timeout after %.1fs", CFG.tts.stream_idle_timeout_s)
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
