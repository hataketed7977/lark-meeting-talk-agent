"""STOP-intent classifier.

Fast path: keyword/regex match against STOP_WORDS.
Used only during TTS playback to decide whether an interruption was the user
telling James to shut up vs. a new request.
"""

from __future__ import annotations

import re
from typing import Iterable

from lark_meeting_voice.wake.detector import normalize

_ASCII_LETTER_RE = re.compile(r"[a-z]")


def _build_stop_pattern(text: str) -> str:
    body = r"\s+".join(re.escape(part) for part in text.split())
    if _ASCII_LETTER_RE.search(text):
        return rf"(?<![a-z]){body}(?![a-z])"
    return body


class StopClassifier:
    def __init__(self, stop_words: Iterable[str]) -> None:
        normalized = [normalize(w) for w in stop_words if w.strip()]
        patterns = [_build_stop_pattern(w) for w in normalized if w]
        self._re = re.compile("|".join(patterns)) if patterns else None

    def is_stop(self, text: str) -> bool:
        if not self._re:
            return False
        return bool(self._re.search(normalize(text)))
