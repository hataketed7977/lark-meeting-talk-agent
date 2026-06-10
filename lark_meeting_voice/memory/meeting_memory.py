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


@dataclass
class MeetingArtifact:
    kind: str
    token: str
    title: str
    meeting_id: str
    created_at: str
    content: str = ""
    fetch_status: str = "pending"
    fetch_error: str = ""


class MeetingMemory:
    _ARTIFACT_EXCERPT_LIMITS = {
        "note": 1200,
        "minute": 1200,
        "verbatim": 800,
    }
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
        self._note: MeetingArtifact | None = None
        self._verbatim: MeetingArtifact | None = None
        self._minute: MeetingArtifact | None = None
        self._meeting_meta: dict[str, str] = {}
        self._seen_event_ids: set[str] = set()
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

    def add_meeting_event(self, event_key: str, payload: dict) -> bool:
        event_id = str(payload.get("event_id") or "").strip()
        if event_id and event_id in self._seen_event_ids:
            return False
        if event_id:
            self._seen_event_ids.add(event_id)

        now = datetime.now(timezone.utc).isoformat()
        if event_key == "vc.meeting.participant_meeting_ended_v1":
            self._meeting_meta = {
                "meeting_id": str(payload.get("meeting_id") or ""),
                "meeting_no": str(payload.get("meeting_no") or ""),
                "topic": str(payload.get("topic") or ""),
                "start_time": str(payload.get("start_time") or ""),
                "end_time": str(payload.get("end_time") or ""),
            }
            return True

        if event_key == "vc.note.generated_v1":
            source = payload.get("note_source") or {}
            meeting_id = str(source.get("source_entity_id") or "")
            note_token = str(payload.get("note_token") or "").strip()
            verbatim_token = str(payload.get("verbatim_token") or "").strip()
            title = str(payload.get("title") or "").strip()
            if note_token:
                self._note = MeetingArtifact(
                    kind="note",
                    token=note_token,
                    title=title,
                    meeting_id=meeting_id,
                    created_at=now,
                )
            if verbatim_token:
                self._verbatim = MeetingArtifact(
                    kind="verbatim",
                    token=verbatim_token,
                    title=title,
                    meeting_id=meeting_id,
                    created_at=now,
                )
            return bool(note_token or verbatim_token)

        if event_key == "minutes.minute.generated_v1":
            source = payload.get("minute_source") or {}
            meeting_id = str(source.get("source_entity_id") or "")
            minute_token = str(payload.get("minute_token") or "").strip()
            title = str(payload.get("title") or "").strip()
            if minute_token:
                self._minute = MeetingArtifact(
                    kind="minute",
                    token=minute_token,
                    title=title,
                    meeting_id=meeting_id,
                    created_at=now,
                )
                return True
            return False

        return False

    def artifacts(self) -> list[MeetingArtifact]:
        return [item for item in (self._note, self._verbatim, self._minute) if item is not None]

    def pending_artifacts(self) -> list[MeetingArtifact]:
        return [item for item in self.artifacts() if item.fetch_status == "pending"]

    def apply_artifact_content(self, kind: str, token: str, content: str) -> bool:
        artifact = self._find_artifact(kind, token)
        if artifact is None:
            return False
        cleaned = self._clean_artifact_content(content)
        artifact.content = cleaned
        artifact.fetch_status = "ready" if cleaned else "empty"
        artifact.fetch_error = ""
        if cleaned:
            self._extract_fact(cleaned, artifact.created_at)
        return True

    def apply_artifact_error(self, kind: str, token: str, error: str) -> bool:
        artifact = self._find_artifact(kind, token)
        if artifact is None:
            return False
        artifact.fetch_status = "error"
        artifact.fetch_error = " ".join((error or "").strip().split())
        return True

    def mark_artifact_fetching(self, kind: str, token: str) -> bool:
        artifact = self._find_artifact(kind, token)
        if artifact is None or artifact.fetch_status != "pending":
            return False
        artifact.fetch_status = "fetching"
        return True

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
        if self._meeting_meta:
            parts.append("Current meeting event metadata:")
            if self._meeting_meta.get("meeting_id"):
                parts.append(f"- Meeting ID: {self._meeting_meta['meeting_id']}")
            if self._meeting_meta.get("meeting_no"):
                parts.append(f"- Meeting number: {self._meeting_meta['meeting_no']}")
            if self._meeting_meta.get("topic"):
                parts.append(f"- Topic: {self._meeting_meta['topic']}")
            if self._meeting_meta.get("start_time"):
                parts.append(f"- Started at: {self._meeting_meta['start_time']}")
            if self._meeting_meta.get("end_time"):
                parts.append(f"- Ended at: {self._meeting_meta['end_time']}")
        artifacts = [
            self._note,
            self._verbatim,
            self._minute,
        ]
        artifacts = [item for item in artifacts if item is not None]
        if artifacts:
            parts.append("Current meeting generated artifacts:")
            for artifact in artifacts:
                title = f" title={artifact.title}" if artifact.title else ""
                status = f" status={artifact.fetch_status}"
                item = f"- {artifact.kind} token={artifact.token}{title}{status}".rstrip()
                if artifact.fetch_error:
                    item += f" error={artifact.fetch_error}"
                parts.append(item)
            excerpts = self._artifact_excerpts()
            if excerpts:
                parts.append("Current meeting artifact excerpts:")
                parts.extend(excerpts)
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

    def _artifact_excerpts(self) -> list[str]:
        parts: list[str] = []
        for artifact in self.artifacts():
            if artifact.fetch_status != "ready" or not artifact.content:
                continue
            limit = self._ARTIFACT_EXCERPT_LIMITS.get(artifact.kind, 1000)
            excerpt = artifact.content[:limit].strip()
            if len(artifact.content) > limit:
                excerpt = excerpt.rstrip() + "..."
            title = f" ({artifact.title})" if artifact.title else ""
            parts.append(f"- {artifact.kind}{title}: {excerpt}")
        return parts

    def _find_artifact(self, kind: str, token: str) -> MeetingArtifact | None:
        for artifact in self.artifacts():
            if artifact.kind == kind and artifact.token == token:
                return artifact
        return None

    def _clean_artifact_content(self, content: str) -> str:
        text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = [line.strip() for line in text.split("\n")]
        cleaned = "\n".join(line for line in lines if line)
        return cleaned.strip()

    def clear(self) -> None:
        self._all.clear()
        self._recent.clear()
        self._actions.clear()
        self._decisions.clear()
        self._risks.clear()
        self._questions.clear()
        self._note = None
        self._verbatim = None
        self._minute = None
        self._meeting_meta = {}
        self._seen_event_ids.clear()
        self._rolling_summary = ""
        self._summary_cursor = 0
