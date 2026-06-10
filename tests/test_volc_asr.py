import asyncio

from lark_meeting_voice.asr.volc_asr import (
    VolcASR,
    _build_asr_start_request,
    _normalize_asr_text,
)
from lark_meeting_voice.config import CFG


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


def test_build_asr_start_request_includes_language_for_v3(monkeypatch):
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    request = _build_asr_start_request("req-2")

    assert request["audio"]["language"] == "en-US"
    assert request["request"]["language"] == "en-US"
    assert request["request"]["model_name"] == "bigmodel"


def test_normalize_asr_text_converts_fullwidth_punctuation_for_english():
    assert _normalize_asr_text("hey James， can you hear me。") == (
        "hey James, can you hear me."
    )
    assert _normalize_asr_text("你好，James。") == "你好，James。"
