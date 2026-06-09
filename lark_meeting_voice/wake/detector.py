"""Wake-word detector.

Matches "hey james", "james", and CJK-friendly variants (e.g. "嘿james", "嘿 james").
Normalization: lower-case, strip punctuation, collapse whitespace.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

# Strip common punctuation including CJK forms.
_PUNCT_RE = re.compile(r"[\s\.,!?;:　，．！？；：、。\-_'\"`~()\[\]{}/]+")
_ASCII_LETTER_RE = re.compile(r"[a-z]")


def normalize(text: str) -> str:
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).lower()
    text = _PUNCT_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class _WakePattern:
    normalized: str
    compact: str
    compact_prefix_re: re.Pattern[str]


def _build_compact_prefix_re(text: str) -> re.Pattern[str]:
    if _ASCII_LETTER_RE.search(text):
        return re.compile(rf"^{re.escape(text)}(?![a-z])")
    return re.compile(rf"^{re.escape(text)}")


class WakeDetector:
    def __init__(self, wake_words: Iterable[str]) -> None:
        patterns: list[_WakePattern] = []
        for wake in wake_words:
            normalized = normalize(wake)
            if not normalized:
                continue
            compact = re.sub(r"\s+", "", normalized)
            patterns.append(
                _WakePattern(normalized, compact, _build_compact_prefix_re(compact))
            )
        self._patterns = sorted(patterns, key=lambda p: len(p.compact), reverse=True)

    def is_wake(self, text: str) -> bool:
        n = normalize(text)
        if not n:
            return False
        compact = re.sub(r"\s+", "", n)
        for p in self._patterns:
            if n == p.normalized or n.startswith(p.normalized + " "):
                return True
            if p.compact_prefix_re.match(compact):
                return True
        return False

    def strip_wake(self, text: str) -> str:
        """Remove the wake word from the start so the LLM sees the real query."""
        n = normalize(text)
        compact = re.sub(r"\s+", "", n)
        for p in self._patterns:
            if n == p.normalized:
                return ""
            if n.startswith(p.normalized + " "):
                return n[len(p.normalized) :].strip()
            match = p.compact_prefix_re.match(compact)
            if match:
                return compact[match.end() :].strip()
        return text
