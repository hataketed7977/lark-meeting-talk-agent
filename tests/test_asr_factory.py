from lark_meeting_voice.asr.factory import create_asr_backend
from lark_meeting_voice.asr.sdk_asr import SDKVolcASR, _build_sdk_request_payload
from lark_meeting_voice.asr.volc_asr import VolcASR
from lark_meeting_voice.config import CFG


def test_factory_uses_sdk_backend_for_v3(monkeypatch):
    monkeypatch.setattr(CFG.asr, "backend", "sdk")
    monkeypatch.setattr(
        CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    )

    backend = create_asr_backend()

    assert isinstance(backend, SDKVolcASR)


def test_factory_falls_back_to_legacy_for_v2(monkeypatch):
    monkeypatch.setattr(CFG.asr, "backend", "sdk")
    monkeypatch.setattr(CFG.asr, "ws_url", "wss://openspeech.bytedance.com/api/v2/asr")

    backend = create_asr_backend()

    assert isinstance(backend, VolcASR)


def test_build_sdk_request_payload_includes_language(monkeypatch):
    monkeypatch.setattr(CFG.asr, "appid", "app")
    monkeypatch.setattr(CFG.asr, "token", "token")
    monkeypatch.setattr(CFG.asr, "cluster", "volcengine_streaming_common")
    monkeypatch.setattr(CFG.asr, "language", "en-US")

    payload = _build_sdk_request_payload()

    assert payload["audio"]["language"] == "en-US"
    assert payload["request"]["model_name"] == "bigmodel"
    assert "language" not in payload["request"]
