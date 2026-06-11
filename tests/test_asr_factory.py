import asyncio
import time

from lark_meeting_voice.asr.factory import create_asr_backend
from lark_meeting_voice.asr.sdk_asr import (
    ASR_KEEPALIVE_INTERVAL_S,
    ASR_KEEPALIVE_POLL_S,
    SDKVolcASR,
    _build_sdk_request_payload,
)
from lark_meeting_voice.config import CFG


def test_factory_uses_sdk_backend(monkeypatch):
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )

    backend = create_asr_backend()

    assert isinstance(backend, SDKVolcASR)


def test_build_sdk_request_payload_omits_language_for_v3_async(monkeypatch):
    monkeypatch.setattr(CFG.asr, "appid", "app")
    monkeypatch.setattr(CFG.asr, "token", "token")
    monkeypatch.setattr(CFG.asr, "cluster", "volcengine_streaming_common")
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    payload = _build_sdk_request_payload()

    assert "language" not in payload["audio"]
    assert payload["request"]["model_name"] == "bigmodel"
    assert payload["request"]["enable_ddc"] is True
    assert payload["request"]["enable_nonstream"] is False


def test_build_sdk_request_payload_includes_language_for_v3_nostream(monkeypatch):
    monkeypatch.setattr(CFG.asr, "appid", "app")
    monkeypatch.setattr(CFG.asr, "token", "token")
    monkeypatch.setattr(CFG.asr, "cluster", "volcengine_streaming_common")
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
    )
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    payload = _build_sdk_request_payload()

    assert payload["audio"]["language"] == "en-US"
    assert "language" not in payload["request"]


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send(self, frame: bytes) -> None:
        self.sent.append(frame)


def test_sdk_buffers_downstream_audio_to_configured_chunk(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(CFG.asr, "sample_rate", 16000)
        monkeypatch.setattr(CFG.asr, "feed_chunk_ms", 200)
        monkeypatch.setattr(
            "lark_meeting_voice.asr.sdk_asr.VolcengineAsrFunctionsV3.generate_asr_audio_only_request",
            lambda **kwargs: kwargs["audio"],
        )
        asr = SDKVolcASR()
        ws = _FakeWebSocket()
        asr._ws = ws  # noqa: SLF001
        asr._closed = asyncio.Event()  # noqa: SLF001

        await asr.feed_pcm(b"a" * 3200)
        assert ws.sent == []

        await asr.feed_pcm(b"b" * 3200)
        assert ws.sent == [b"a" * 3200 + b"b" * 3200]

    asyncio.run(run())


def test_sdk_silence_keepalive_sends_when_downstream_pauses(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(CFG.asr, "sample_rate", 16000)
        asr = SDKVolcASR()
        ws = _FakeWebSocket()
        asr._ws = ws  # noqa: SLF001
        asr._closed = asyncio.Event()  # noqa: SLF001
        asr._next_sequence = 1  # noqa: SLF001
        asr._last_audio_sent_at = time.monotonic() - ASR_KEEPALIVE_INTERVAL_S - 0.1  # noqa: SLF001
        asr._silence_frame = b"\x00" * 3200  # noqa: SLF001

        task = asyncio.create_task(asr._silence_keepalive_loop())  # noqa: SLF001
        await asyncio.sleep(ASR_KEEPALIVE_POLL_S + 0.1)
        asr._closed.set()  # noqa: SLF001
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert ws.sent

    asyncio.run(run())


def test_sdk_silence_keepalive_does_not_flood_recent_audio(monkeypatch):
    async def run() -> None:
        monkeypatch.setattr(CFG.asr, "sample_rate", 16000)
        asr = SDKVolcASR()
        ws = _FakeWebSocket()
        asr._ws = ws  # noqa: SLF001
        asr._closed = asyncio.Event()  # noqa: SLF001
        asr._next_sequence = 1  # noqa: SLF001
        asr._last_audio_sent_at = time.monotonic()  # noqa: SLF001
        asr._silence_frame = b"\x00" * 3200  # noqa: SLF001

        task = asyncio.create_task(asr._silence_keepalive_loop())  # noqa: SLF001
        await asyncio.sleep(ASR_KEEPALIVE_POLL_S + 0.1)
        asr._closed.set()  # noqa: SLF001
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

        assert ws.sent == []

    asyncio.run(run())


def test_sdk_fallback_dispatches_utterance_additions_text():
    async def run() -> None:
        partials: list[str] = []

        async def on_partial(text: str) -> None:
            partials.append(text)

        asr = SDKVolcASR(on_partial=on_partial)
        await asr._dispatch_fallback_result(  # noqa: SLF001
            {"is_last_package": False},
            {
                "additions": {"log_id": "log"},
                "utterances": [
                    {
                        "additions": {"text": "How do you feel about this meeting?"},
                        "definite": False,
                    }
                ],
            },
        )

        assert partials == ["How do you feel about this meeting?"]

    asyncio.run(run())


def test_sdk_fallback_dispatches_definite_utterance_as_final():
    async def run() -> None:
        finals: list[str] = []

        async def on_final(text: str) -> None:
            finals.append(text)

        asr = SDKVolcASR(on_final=on_final)
        await asr._dispatch_fallback_result(  # noqa: SLF001
            {"is_last_package": False},
            {
                "utterances": [
                    {
                        "additions": {"text": "The final answer."},
                        "definite": True,
                    }
                ],
            },
        )

        assert finals == ["The final answer."]

    asyncio.run(run())
