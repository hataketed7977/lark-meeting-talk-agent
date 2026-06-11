"""ASR backend factory."""

from __future__ import annotations

from lark_meeting_voice.asr.base import OnError, OnText, SpeechRecognizer
from lark_meeting_voice.asr.sdk_asr import SDKVolcASR


def create_asr_backend(
    *,
    on_partial: OnText | None = None,
    on_final: OnText | None = None,
    on_error: OnError | None = None,
) -> SpeechRecognizer:
    return SDKVolcASR(
        on_partial=on_partial,
        on_final=on_final,
        on_error=on_error,
    )
