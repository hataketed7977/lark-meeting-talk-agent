"""ASR backend factory."""

from __future__ import annotations

import logging

from lark_meeting_voice.asr.base import OnError, OnText, SpeechRecognizer
from lark_meeting_voice.asr.sdk_asr import SDKVolcASR
from lark_meeting_voice.asr.volc_asr import VolcASR
from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)


def create_asr_backend(
    *,
    on_partial: OnText | None = None,
    on_final: OnText | None = None,
    on_error: OnError | None = None,
) -> SpeechRecognizer:
    backend = CFG.asr.backend.strip().lower()
    if backend == "sdk":
        if "/api/v3/" not in CFG.asr.ws_url:
            log.warning(
                "ASR backend sdk requires v3 endpoint; falling back to legacy backend"
            )
        else:
            return SDKVolcASR(
                on_partial=on_partial,
                on_final=on_final,
                on_error=on_error,
            )
    return VolcASR(
        on_partial=on_partial,
        on_final=on_final,
        on_error=on_error,
    )
