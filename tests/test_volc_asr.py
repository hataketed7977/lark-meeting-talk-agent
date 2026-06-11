import asyncio

from lark_meeting_voice.asr.volc_asr import (
    VolcASR,
    _build_asr_start_request,
    _normalize_asr_text,
)
from lark_meeting_voice.config import CFG


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []
        self.close_calls = 0
        self.close_code = 1000
        self.close_reason = "normal"

    async def send(self, frame: bytes) -> None:
        self.sent.append(frame)

    async def close(self) -> None:
        self.close_calls += 1


def test_handle_result_treats_explicit_final_without_utterances_as_final():
    seen: list[str] = []

    async def on_final(text: str) -> None:
        seen.append(text)

    async def run() -> None:
        asr = VolcASR(on_final=on_final)
        await asr._handle_result(  # noqa: SLF001
            {
                "sequence": -1,
                "payload": {
                    "result": [
                        {
                            "text": "hey james are you there",
                            "utterances": [],
                        }
                    ]
                },
            }
        )

    asyncio.run(run())
    assert seen == ["hey james are you there"]


def test_build_asr_start_request_includes_language_for_v2(monkeypatch):
    monkeypatch.setattr(CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v2/asr")
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    request = _build_asr_start_request("req-1")

    assert request["audio"]["language"] == "en-US"
    assert request["request"]["reqid"] == "req-1"
    assert "language" not in request["request"]


def test_build_asr_start_request_omits_language_for_v3_async(monkeypatch):
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    request = _build_asr_start_request("req-2")

    assert "language" not in request["audio"]
    assert "language" not in request["request"]
    assert request["request"]["model_name"] == "bigmodel"


def test_build_asr_start_request_includes_language_for_v3_nostream(monkeypatch):
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream"
    )
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    request = _build_asr_start_request("req-3")

    assert request["audio"]["language"] == "en-US"
    assert "language" not in request["request"]


def test_normalize_asr_text_converts_fullwidth_punctuation_for_english():
    assert _normalize_asr_text("hey James， can you hear me。") == (
        "hey James, can you hear me."
    )
    assert _normalize_asr_text("你好，James。") == "你好，James。"


def test_stop_is_idempotent_and_sends_final_frame_once():
    async def run() -> None:
        asr = VolcASR()
        fake_ws = _FakeWebSocket()
        asr._ws = fake_ws  # noqa: SLF001
        asr._recv_task = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001

        await asr.stop()
        await asr.stop()

        assert len(fake_ws.sent) == 1
        assert fake_ws.close_calls == 1
        assert asr._ws is None  # noqa: SLF001

    asyncio.run(run())


def test_feed_pcm_after_stop_does_not_send_more_audio():
    async def run() -> None:
        asr = VolcASR()
        fake_ws = _FakeWebSocket()
        asr._ws = fake_ws  # noqa: SLF001
        asr._recv_task = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001

        await asr.stop()
        await asr.feed_pcm(b"\x00" * 3200)

        assert len(fake_ws.sent) == 1
        assert fake_ws.close_calls == 1

    asyncio.run(run())


def test_stop_works_with_client_connection_like_object_without_closed_attr():
    async def run() -> None:
        asr = VolcASR()
        fake_ws = _FakeWebSocket()
        asr._ws = fake_ws  # noqa: SLF001
        asr._recv_task = asyncio.create_task(asyncio.sleep(0))  # noqa: SLF001

        await asr.stop()

        assert len(fake_ws.sent) == 1
        assert fake_ws.close_calls == 1

    asyncio.run(run())
