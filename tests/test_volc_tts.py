import base64
import gzip
import json
import struct
import asyncio
from typing import Optional

from lark_meeting_voice.config import CFG
from lark_meeting_voice.tts.volc_tts import (
    AUDIO_ONLY_RESPONSE,
    COMPRESSION_NONE,
    CONNECTION_STARTED_EVENT,
    FINISH_CONNECTION_EVENT,
    FINISH_SESSION_EVENT,
    FULL_CLIENT_REQUEST,
    FULL_SERVER_RESPONSE,
    SERIALIZATION_RAW,
    SESSION_FINISHED_EVENT,
    SESSION_STARTED_EVENT,
    START_CONNECTION_EVENT,
    START_SESSION_EVENT,
    TASK_REQUEST_EVENT,
    WITH_EVENT,
    VolcTTS,
    _build_ws_v3_headers,
    _decode_http_v3_audio,
    _header,
    _parse_ws_v3_message,
    _pop_stream_json_objects,
)


def test_pop_stream_json_objects_parses_sse_and_concatenated_json():
    audio = base64.b64encode(b"pcm").decode("ascii")
    buf = f'data: {{"code":0,"sequence":1,"data":"{audio}"}}\n{{"sequence":-1}}\n'

    items, rest = _pop_stream_json_objects(buf)

    assert rest == ""
    assert items == [
        {"code": 0, "sequence": 1, "data": audio},
        {"sequence": -1},
    ]


def test_decode_http_v3_audio_returns_pcm_and_done_signal():
    audio = base64.b64encode(b"pcm").decode("ascii")

    chunk, done = _decode_http_v3_audio({"code": 0, "sequence": "1", "data": audio})
    final_chunk, final_done = _decode_http_v3_audio({"sequence": -1})

    assert chunk == b"pcm"
    assert done is False
    assert final_chunk == b""
    assert final_done is True


def test_decode_http_v3_audio_treats_success_without_data_as_clean_end():
    chunk, done = _decode_http_v3_audio(
        {"code": 20000000, "message": "OK", "data": None}
    )

    assert chunk == b""
    assert done is True


def test_build_ws_v3_headers_prefers_api_key(monkeypatch):
    monkeypatch.setattr(CFG.tts, "api_key", "api-key")
    monkeypatch.setattr(CFG.tts, "appid", "appid")
    monkeypatch.setattr(CFG.tts, "token", "token")
    monkeypatch.setattr(CFG.tts, "resource_id", "seed-tts-2.0")

    headers = _build_ws_v3_headers("connect-1")

    assert headers["X-Api-Key"] == "api-key"
    assert headers["X-Api-Resource-Id"] == "seed-tts-2.0"
    assert headers["X-Api-Connect-Id"] == "connect-1"
    assert "X-Api-App-Key" not in headers


def test_parse_ws_v3_audio_and_control_frames():
    audio_frame = (
        _header(
            AUDIO_ONLY_RESPONSE,
            flags=WITH_EVENT,
            serialization=SERIALIZATION_RAW,
            compression=COMPRESSION_NONE,
        )
        + struct.pack(">I", 352)
        + struct.pack(">I", 3)
        + b"pcm"
    )
    payload = gzip.compress(
        json.dumps({"event": SESSION_FINISHED_EVENT}).encode("utf-8")
    )
    control_frame = (
        _header(FULL_SERVER_RESPONSE, flags=WITH_EVENT)
        + struct.pack(">I", SESSION_FINISHED_EVENT)
        + struct.pack(">I", len(payload))
        + payload
    )

    parsed_audio = _parse_ws_v3_message(audio_frame)
    parsed_control = _parse_ws_v3_message(control_frame)

    assert parsed_audio["kind"] == "audio"
    assert parsed_audio["audio"] == b"pcm"
    assert parsed_control["kind"] == "control"
    assert parsed_control["event"] == SESSION_FINISHED_EVENT


class _FakeWebSocket:
    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []
        self.response_headers = {"X-Tt-Logid": "log-1"}
        self.closed = False

    async def send(self, data):
        self.sent.append(data)

    async def recv(self):
        if not self._responses:
            raise RuntimeError("no more responses")
        return self._responses.pop(0)

    async def close(self):
        self.closed = True


def _server_control_frame(event: int, payload: Optional[dict] = None) -> bytes:
    body = gzip.compress(json.dumps(payload or {"event": event}).encode("utf-8"))
    return (
        _header(FULL_SERVER_RESPONSE, flags=WITH_EVENT)
        + struct.pack(">I", event)
        + struct.pack(">I", len(body))
        + body
    )


def _server_audio_frame(audio: bytes) -> bytes:
    return (
        _header(
            AUDIO_ONLY_RESPONSE,
            flags=WITH_EVENT,
            serialization=SERIALIZATION_RAW,
            compression=COMPRESSION_NONE,
        )
        + struct.pack(">I", 352)
        + struct.pack(">I", len(audio))
        + audio
    )


def _client_frame_event(frame: bytes) -> int:
    assert (frame[1] >> 4) & 0x0F == FULL_CLIENT_REQUEST
    cursor = (frame[0] & 0x0F) * 4
    return struct.unpack(">I", frame[cursor : cursor + 4])[0]


def test_synthesize_stream_ws_v3_uses_single_session(monkeypatch):
    async def _run() -> None:
        monkeypatch.setattr(CFG.tts, "mode", "ws_v3")
        monkeypatch.setattr(
            CFG.tts,
            "ws_url",
            "wss://openspeech.bytedance.com/api/v3/tts/bidirection",
        )
        ws = _FakeWebSocket(
            [
                _server_control_frame(CONNECTION_STARTED_EVENT),
                _server_control_frame(SESSION_STARTED_EVENT),
                _server_audio_frame(b"pcm"),
                _server_control_frame(SESSION_FINISHED_EVENT),
            ]
        )

        async def _fake_connect(*_args, **_kwargs):
            return ws

        monkeypatch.setattr(
            "lark_meeting_voice.tts.volc_tts.websockets.connect", _fake_connect
        )

        async def _stream():
            yield "hello"
            yield " world"

        cancel_event = asyncio.Event()
        tts = VolcTTS()
        output = []
        async for chunk in tts.synthesize_stream(_stream(), cancel_event):
            output.append(chunk)

        assert output == [b"pcm"]
        assert ws.closed is True
        assert [_client_frame_event(frame) for frame in ws.sent] == [
            START_CONNECTION_EVENT,
            START_SESSION_EVENT,
            TASK_REQUEST_EVENT,
            TASK_REQUEST_EVENT,
            FINISH_SESSION_EVENT,
            FINISH_CONNECTION_EVENT,
        ]

    asyncio.run(_run())
