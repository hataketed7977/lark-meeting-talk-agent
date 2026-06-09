from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Deque


@dataclass
class MeetingUtterance:
    text: str
    created_at: str
    source: str = "meeting_asr"


@dataclass
class MeetingFact:
    kind: str
    text: str
    created_at: str


class MeetingMemory:
    _ACTION_RE = re.compile(
        r"(todo|action item|follow up|follow-up|owner|deadline|负责|跟进|待办|行动项|下周|明天|今天内)",
        re.IGNORECASE,
    )
    _DECISION_RE = re.compile(
        r"(decide|decision|agreed|we will|定了|决定|结论|方案是|一致认为)",
        re.IGNORECASE,
    )
    _RISK_RE = re.compile(
        r"(risk|blocker|issue|problem|concern|风险|阻塞|问题|卡点|挑战)",
        re.IGNORECASE,
    )
    _QUESTION_RE = re.compile(
        r"(question|unknown|not sure|待确认|不确定|有没有|是否|吗|？|\?)",
        re.IGNORECASE,
    )
    _TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{2,}", re.IGNORECASE)

    def __init__(
        self, max_recent_utterances: int = 50, max_summary_items: int = 8
    ) -> None:
        self._all: list[MeetingUtterance] = []
        self._recent: Deque[MeetingUtterance] = deque(maxlen=max_recent_utterances)
        self._actions: Deque[MeetingFact] = deque(maxlen=max_summary_items)
        self._decisions: Deque[MeetingFact] = deque(maxlen=max_summary_items)
        self._risks: Deque[MeetingFact] = deque(maxlen=max_summary_items)
        self._questions: Deque[MeetingFact] = deque(maxlen=max_summary_items)
        self._rolling_summary = ""
        self._summary_cursor = 0

    def add_transcript(self, text: str, source: str = "meeting_asr") -> None:
        cleaned = " ".join((text or "").strip().split())
        if not cleaned:
            return

        now = datetime.now(timezone.utc).isoformat()
        utterance = MeetingUtterance(text=cleaned, created_at=now, source=source)
        self._all.append(utterance)
        self._recent.append(utterance)
        self._extract_fact(cleaned, now)

    def _extract_fact(self, text: str, created_at: str) -> None:
        if self._ACTION_RE.search(text):
            self._actions.append(
                MeetingFact(kind="action", text=text, created_at=created_at)
            )
        if self._DECISION_RE.search(text):
            self._decisions.append(
                MeetingFact(kind="decision", text=text, created_at=created_at)
            )
        if self._RISK_RE.search(text):
            self._risks.append(
                MeetingFact(kind="risk", text=text, created_at=created_at)
            )
        if self._QUESTION_RE.search(text):
            self._questions.append(
                MeetingFact(kind="question", text=text, created_at=created_at)
            )

    def recent_utterances(self) -> list[MeetingUtterance]:
        return list(self._recent)

    @property
    def utterance_count(self) -> int:
        return len(self._all)

    @property
    def rolling_summary(self) -> str:
        return self._rolling_summary

    def needs_rollup(self, every_utterances: int) -> bool:
        if every_utterances <= 0:
            return False
        return len(self._all) - self._summary_cursor >= every_utterances

    def unsummarized_transcript(self, max_chars: int) -> str:
        items = self._all[self._summary_cursor :]
        lines = [f"- {item.text}" for item in items]
        text = "\n".join(lines)
        if max_chars > 0 and len(text) > max_chars:
            return text[-max_chars:]
        return text

    def apply_rolling_summary(self, summary: str, max_chars: int) -> None:
        cleaned = " ".join((summary or "").strip().split())
        if max_chars > 0 and len(cleaned) > max_chars:
            cleaned = cleaned[-max_chars:]
        if cleaned:
            self._rolling_summary = cleaned
            self._summary_cursor = len(self._all)

    def summary_snapshot(self) -> dict[str, list[str]]:
        return {
            "decisions": [item.text for item in self._decisions],
            "actions": [item.text for item in self._actions],
            "risks": [item.text for item in self._risks],
            "open_questions": [item.text for item in self._questions],
        }

    def build_context_block(
        self,
        query: str | None = None,
        *,
        recent_limit: int = 16,
        retrieval_limit: int = 6,
    ) -> str:
        parts: list[str] = [
            "Meeting memory snapshot:",
            f"- Total captured utterances: {len(self._all)}",
        ]
        snapshot = self.summary_snapshot()

        if self._rolling_summary:
            parts.append("Rolling summary:")
            parts.append(self._rolling_summary)
        if snapshot["decisions"]:
            parts.append("Decisions:")
            parts.extend(f"- {item}" for item in snapshot["decisions"])
        if snapshot["actions"]:
            parts.append("Action items:")
            parts.extend(f"- {item}" for item in snapshot["actions"])
        if snapshot["risks"]:
            parts.append("Risks / blockers:")
            parts.extend(f"- {item}" for item in snapshot["risks"])
        if snapshot["open_questions"]:
            parts.append("Open questions:")
            parts.extend(f"- {item}" for item in snapshot["open_questions"])
        evidence = self._retrieve_evidence(query or "", retrieval_limit)
        if evidence:
            parts.append("Relevant earlier transcript:")
            parts.extend(f"- {item.text}" for item in evidence)
        if self._recent:
            parts.append("Recent transcript:")
            recent = list(self._recent)[-recent_limit:] if recent_limit > 0 else []
            parts.extend(f"- {item.text}" for item in recent)

        if len(parts) == 2:
            return "Meeting memory snapshot:\n- No transcript has been captured yet."
        return "\n".join(parts)

    def _retrieve_evidence(self, query: str, limit: int) -> list[MeetingUtterance]:
        if limit <= 0 or not query.strip() or not self._all:
            return []
        terms = {m.group(0).lower() for m in self._TOKEN_RE.finditer(query)}
        if not terms:
            return []

        recent_ids = {id(item) for item in self._recent}
        scored: list[tuple[int, int, MeetingUtterance]] = []
        for idx, item in enumerate(self._all):
            if id(item) in recent_ids:
                continue
            lower = item.text.lower()
            score = sum(1 for term in terms if term in lower)
            if score > 0:
                scored.append((score, idx, item))

        scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
        selected = sorted(scored[:limit], key=lambda row: row[1])
        return [item for _, _, item in selected]

    def clear(self) -> None:
        self._all.clear()
        self._recent.clear()
        self._actions.clear()
        self._decisions.clear()
        self._risks.clear()
        self._questions.clear()
        self._rolling_summary = ""
        self._summary_cursor = 0
