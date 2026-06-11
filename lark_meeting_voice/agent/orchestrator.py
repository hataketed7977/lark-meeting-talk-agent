"""Core agent orchestrator.

Behavior:
    - WAITING: always listens and writes meeting memory, but never replies.
    - Wake word opens an ENGAGED conversation session.
    - ENGAGED: user can continue talking without repeating the wake word.
    - SPEAKING: any user speech can barge in and cancel current playback.
    - Explicit end-session words return the bot to WAITING.

This module owns the state and wires together: RealtimeClient, VolcASR,
OpenAICompatibleLLM, VolcTTS, WakeDetector, StopClassifier, PacedSender.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import re
import struct
import time
from typing import Optional

from lark_meeting_voice.asr.base import SpeechRecognizer
from lark_meeting_voice.asr.factory import create_asr_backend
from lark_meeting_voice.audio.framer import PacedSender, split_pcm
from lark_meeting_voice.audio.resample import downsample_24k_to_16k
from lark_meeting_voice.config import CFG
from lark_meeting_voice.intent.stop_classifier import StopClassifier
from lark_meeting_voice.lark.artifact_fetcher import (
    ArtifactFetchError,
    fetch_artifact_content,
)
from lark_meeting_voice.lark.event_consumer import (
    CurrentMeetingEventConsumers,
    MeetingEvent,
)
from lark_meeting_voice.lark.realtime import RealtimeClient
from lark_meeting_voice.llm.openai_compatible import (
    OpenAICompatibleLLM,
)
from lark_meeting_voice.knowledge_routes import (
    build_doc_context,
    canonicalize_doc_query,
    match_doc_route,
)
from lark_meeting_voice.memory.meeting_memory import MeetingMemory
from lark_meeting_voice.tts.volc_tts import VolcTTS
from lark_meeting_voice.wake.detector import WakeDetector

log = logging.getLogger(__name__)


def _pcm16_metrics(pcm: bytes) -> tuple[int, int]:
    if not pcm:
        return 0, 0
    count = len(pcm) // 2
    if count <= 0:
        return 0, 0
    samples = struct.unpack("<" + "h" * count, pcm[: count * 2])
    peak = max(abs(s) for s in samples)
    rms = int(math.sqrt(sum(s * s for s in samples) / count))
    return peak, rms


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


_LOW_VALUE_QUERY_WORDS = {
    "ah",
    "aha",
    "er",
    "erm",
    "hm",
    "hmm",
    "huh",
    "mm",
    "mmm",
    "oh",
    "ooh",
    "uh",
    "uhh",
    "um",
    "umm",
}

_SUMMARY_QUERY_PATTERNS = (
    re.compile(
        r"\b(summarize|summary|recap|wrap up|overview|takeaways?|minutes?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(action items?|next steps?|follow[- ]ups?|decisions?|risks?|blockers?|open questions?)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(evaluate|evaluation|how was|how did)\b.*\b(meeting|sharing|presentation|discussion|review)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(总结|概括|回顾|复盘|这次会|这个会议|纪要|待办|行动项|结论|风险|问题)"
    ),
)


def _is_low_value_query(text: str) -> bool:
    raw = " ".join((text or "").strip().split())
    if not raw:
        return True
    if _contains_cjk(raw):
        return False
    normalized = re.sub(r"[^a-z]+", " ", raw.lower()).strip()
    if not normalized:
        return True
    words = normalized.split()
    return len(words) == 1 and words[0] in _LOW_VALUE_QUERY_WORDS


_INCOMPLETE_ENGLISH_TAIL_PATTERNS = (
    re.compile(r"\b(how do you feel about|what do you think about)$", re.IGNORECASE),
    re.compile(r"\b(my question is|the question is|i mean|i want|i want to)$", re.IGNORECASE),
    re.compile(r"\b(can you help me to|could you help me to|help me to)$", re.IGNORECASE),
    re.compile(
        r"\b(about|because|for|from|if|is|of|or|so|that|the|to|with)$",
        re.IGNORECASE,
    ),
)


def _has_incomplete_english_tail(text: str) -> bool:
    if _contains_cjk(text):
        return False
    normalized = re.sub(r"[^a-z']+", " ", (text or "").lower()).strip()
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in _INCOMPLETE_ENGLISH_TAIL_PATTERNS)


def _final_key(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _is_summary_query(text: str) -> bool:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return False
    return any(pattern.search(cleaned) for pattern in _SUMMARY_QUERY_PATTERNS)


class State(str, enum.Enum):
    WAITING = "waiting"
    ENGAGED = "engaged"
    SPEAKING = "speaking"


class Orchestrator:
    def __init__(
        self,
        realtime: RealtimeClient,
        *,
        meeting_id: str | None = None,
        memory: MeetingMemory | None = None,
    ) -> None:
        self._rt = realtime
        self._meeting_id = meeting_id
        self._wake = WakeDetector(CFG.agent.wake_words)
        self._stop = StopClassifier(CFG.agent.stop_words)
        self._end_session = StopClassifier(CFG.agent.end_session_words)
        self._llm = OpenAICompatibleLLM()
        self._tts = VolcTTS()
        self._memory = memory or MeetingMemory(
            max_recent_utterances=CFG.agent.memory_recent_utterances,
            max_summary_items=CFG.agent.memory_summary_items,
        )
        self._asr: Optional[SpeechRecognizer] = None
        self._sender = PacedSender(self._rt.send_audio)

        self._state: State = State.WAITING
        self._state_lock = asyncio.Lock()
        self._cancel_event = asyncio.Event()  # signals current reply to abort
        self._reply_task: Optional[asyncio.Task] = None
        self._summary_task: Optional[asyncio.Task] = None
        # Barge-in gating:
        #   The user's wake utterance ("hey James, ...") often leaves an ASR
        #   tail partial arriving 1-3s AFTER Reply START. If we treat every
        #   partial as a barge-in, the reply is cancelled before the first
        #   LLM token returns and the bot stays silent.
        #   So: only accept barge-in once TTS has actually fed audio, AND at
        #   least MIN_REPLY_AUDIBLE_S has elapsed since first audio feed.
        self._tts_audio_started: bool = False
        self._tts_started_at: float = 0.0
        # How long the reply is protected from barge-in after the first TTS
        # frame is queued. Tune up if users keep accidentally talking over.
        self.MIN_REPLY_AUDIBLE_S: float = 1.5
        self._reply_started_at: float = 0.0
        self._reply_barge_in_count: int = 0
        self._engaged_last_active_at: float = 0.0
        self._event_consumers: CurrentMeetingEventConsumers | None = None
        self._artifact_tasks: dict[tuple[str, str], asyncio.Task] = {}
        self._shutdown_requested = asyncio.Event()
        self._exit_reason: str | None = None
        self._soft_final_task: Optional[asyncio.Task] = None
        self._latest_partial: str = ""
        self._latest_partial_key: str = ""
        self._pending_soft_final_key: str | None = None
        self._turn_started_at: float = 0.0
        self._latest_partial_started_at: float = 0.0
        self._current_reply_turn_started_at: float = 0.0
        self._sender_started: bool = False

    @property
    def exit_reason(self) -> str | None:
        return self._exit_reason

    @property
    def memory(self) -> MeetingMemory:
        return self._memory

    async def _start_meeting_event_consumers(self) -> None:
        if not self._meeting_id:
            return
        try:
            self._event_consumers = CurrentMeetingEventConsumers(
                self._meeting_id,
                self._on_meeting_event,
            )
            await self._event_consumers.start()
            log.info("Meeting event consumers started meeting_id=%s", self._meeting_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Meeting event consumers disabled after startup error: %s", exc)

    async def start_realtime_audio(self) -> None:
        # Start the paced sender FIRST so we begin streaming silence frames
        # upstream within ~100ms of session.created. The Feishu Realtime
        # server is sensitive to delayed upstream audio at session startup.
        if self._sender_started:
            return
        try:
            await self._sender.start()
            self._sender_started = True
            log.info("PacedSender started")
        except Exception:
            log.exception("Failed to start PacedSender — aborting")
            raise

    async def run(self) -> None:
        await self.start_realtime_audio()

        self._asr = create_asr_backend(
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_error=self._on_asr_error,
        )
        try:
            await self._asr.start()
            log.info("ASR backend started")
        except Exception:
            log.exception("Failed to start ASR backend — aborting orchestrator")
            raise

        # Spawn the downstream pump (Feishu -> ASR).
        pump_task = asyncio.create_task(self._downstream_pump(), name="downstream-pump")
        event_consumers_task: asyncio.Task | None = None
        if self._meeting_id:
            event_consumers_task = asyncio.create_task(
                self._start_meeting_event_consumers(),
                name="meeting-event-consumers-start",
            )

        try:
            while not self._rt._closed.is_set():  # type: ignore[attr-defined]
                if self._shutdown_requested.is_set():
                    break
                await asyncio.sleep(0.5)
                await self._maybe_expire_engaged_session()
        finally:
            pump_task.cancel()
            self._cancel_soft_final_task()
            if event_consumers_task is not None and not event_consumers_task.done():
                event_consumers_task.cancel()
                try:
                    await event_consumers_task
                except (asyncio.CancelledError, Exception):
                    pass
            if self._event_consumers is not None:
                await self._event_consumers.stop()
            for task in self._artifact_tasks.values():
                task.cancel()
            for task in self._artifact_tasks.values():
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
            self._artifact_tasks.clear()
            if self._summary_task is not None:
                self._summary_task.cancel()
                try:
                    await self._summary_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self._abort_reply()
            await self._sender.stop()
            if self._asr is not None:
                await self._asr.stop()

    # ---------------- downstream pump ----------------

    async def _downstream_pump(self) -> None:
        """Read 24 kHz PCM from Feishu -> downsample -> feed ASR."""
        assert self._asr is not None
        chunk_count = 0
        startup_started_at = time.monotonic()
        startup_audio_healthy = False
        startup_fail_window_s = CFG.agent.startup_silent_downstream_fail_window_s
        try:
            async for audio in self._rt.downstream():
                if not audio.pcm:
                    continue
                chunk_count += 1
                peak24k, rms24k = _pcm16_metrics(audio.pcm)
                if not startup_audio_healthy and (peak24k > 0 or rms24k > 0):
                    startup_audio_healthy = True
                    log.info(
                        "Downstream audio became healthy chunk_count=%d peak24k=%d rms24k=%d",
                        chunk_count,
                        peak24k,
                        rms24k,
                    )
                if (
                    not startup_audio_healthy
                    and startup_fail_window_s > 0
                    and time.monotonic() - startup_started_at >= startup_fail_window_s
                ):
                    log.warning(
                        "Downstream audio stayed silent for %.1fs after startup "
                        "chunk_count=%d peak24k=%d rms24k=%d -> forcing rebuild",
                        startup_fail_window_s,
                        chunk_count,
                        peak24k,
                        rms24k,
                    )
                    self._shutdown_requested.set()
                    self._cancel_soft_final_task()
                    self._clear_partial_tracking()
                    await self._rt.fail_recoverably("startup_silent_downstream")
                    return
                if chunk_count == 1 or chunk_count % 100 == 0:
                    log.info(
                        "Downstream pump chunk count=%d pcm24k_bytes=%d duration_ms=%d peak24k=%d rms24k=%d",
                        chunk_count,
                        len(audio.pcm),
                        audio.duration_ms,
                        peak24k,
                        rms24k,
                    )
                pcm16k = downsample_24k_to_16k(audio.pcm)
                await self._asr.feed_pcm(pcm16k)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("downstream pump crashed: %s", e)

    # ---------------- ASR callbacks ----------------

    async def _on_partial(self, text: str) -> None:
        text = " ".join((text or "").strip().split())
        log.debug("ASR partial: %s", text)
        if not text:
            return
        if self._state != State.SPEAKING:
            self._schedule_soft_final(text)
            return
        self._cancel_soft_final_task()
        # Gate barge-in: ignore partials until TTS has actually started
        # speaking AND we've been audible for at least MIN_REPLY_AUDIBLE_S.
        # This swallows the tail of the user's wake utterance and prevents
        # the bot from being cancelled before it can say a single word.
        if not self._tts_audio_started:
            log.debug("Ignoring partial %r — TTS hasn't started speaking yet", text)
            return
        elapsed = time.monotonic() - self._tts_started_at
        if elapsed < self.MIN_REPLY_AUDIBLE_S:
            log.debug(
                "Ignoring partial %r — only %.2fs into reply (need %.2fs)",
                text,
                elapsed,
                self.MIN_REPLY_AUDIBLE_S,
            )
            return
        self._reply_barge_in_count += 1
        log.info(
            "Barge-in detected (partial: %r, t+%.2fs, count=%d) -> aborting reply",
            text,
            elapsed,
            self._reply_barge_in_count,
        )
        await self._abort_reply(next_state=State.ENGAGED)

    async def _on_final(self, text: str) -> None:
        await self._handle_final(text, source="asr_final")

    def _cancel_soft_final_task(self) -> None:
        task = self._soft_final_task
        self._soft_final_task = None
        if task is not None and not task.done():
            task.cancel()

    def _clear_partial_tracking(self) -> None:
        self._latest_partial = ""
        self._latest_partial_key = ""
        self._latest_partial_started_at = 0.0

    def _soft_final_policy(self) -> tuple[float, int]:
        if self._state == State.ENGAGED:
            return (
                CFG.agent.engaged_asr_soft_final_quiet_window_s,
                CFG.agent.engaged_asr_soft_final_min_chars,
            )
        return (
            CFG.agent.asr_soft_final_quiet_window_s,
            CFG.agent.asr_soft_final_min_chars,
        )

    def _should_soft_finalize(self, text: str) -> bool:
        if self._state not in {State.WAITING, State.ENGAGED}:
            return False
        _, min_chars = self._soft_final_policy()
        if len(text) < min_chars:
            return False
        if _is_low_value_query(text):
            return False
        if _has_incomplete_english_tail(text):
            return False
        if self._state == State.WAITING and not self._wake.is_wake(text):
            return False
        return True

    def _mark_turn_activity(self, text: str) -> None:
        now = time.monotonic()
        key = _final_key(text)
        if not self._turn_started_at:
            self._turn_started_at = now
            log.info("Turn activity started state=%s text=%r", self._state.value, text)
        if key != self._latest_partial_key:
            self._latest_partial_started_at = now

    def _schedule_soft_final(self, text: str) -> None:
        if not self._should_soft_finalize(text):
            return
        self._mark_turn_activity(text)
        self._latest_partial = text
        self._latest_partial_key = _final_key(text)
        self._cancel_soft_final_task()
        quiet_window_s, min_chars = self._soft_final_policy()
        self._soft_final_task = asyncio.create_task(
            self._emit_soft_final_after_quiet_window(
                self._latest_partial_key,
                text,
                quiet_window_s,
                min_chars,
            ),
            name="soft-final",
        )

    async def _emit_soft_final_after_quiet_window(
        self,
        expected_key: str,
        text: str,
        quiet_window_s: float,
        min_chars: int,
    ) -> None:
        try:
            await asyncio.sleep(quiet_window_s)
            if self._latest_partial_key != expected_key or self._latest_partial != text:
                return
            current_quiet_window_s, current_min_chars = self._soft_final_policy()
            if (
                current_quiet_window_s != quiet_window_s
                or current_min_chars != min_chars
                or not self._should_soft_finalize(text)
            ):
                return
            self._pending_soft_final_key = expected_key
            self._soft_final_task = None
            partial_age_s = (
                time.monotonic() - self._latest_partial_started_at
                if self._latest_partial_started_at
                else 0.0
            )
            log.info(
                "ASR soft final state=%s quiet_window=%.2fs min_chars=%d partial_age=%.2fs text=%s",
                self._state.value,
                quiet_window_s,
                min_chars,
                partial_age_s,
                text,
            )
            await self._handle_final(text, source="soft_final")
        except asyncio.CancelledError:
            raise

    def _build_meeting_context(self, query: str) -> tuple[str, bool, str | None]:
        doc_route = match_doc_route(query)
        summary_mode = _is_summary_query(query)
        if doc_route:
            context = build_doc_context(
                doc_route,
                query=query,
                max_chars=CFG.agent.doc_context_max_chars,
            )
        elif summary_mode:
            context = self._memory.build_summary_context_block(
                query,
                max_chars=CFG.agent.summary_context_max_chars,
                summary_max_chars=CFG.agent.summary_context_summary_max_chars,
                facts_max_chars=CFG.agent.summary_context_facts_max_chars,
                artifact_max_chars=CFG.agent.summary_context_artifact_max_chars,
                retrieval_limit=CFG.agent.summary_context_retrieval_max_items,
            )
        else:
            context = self._memory.build_context_block(
                query,
                recent_limit=CFG.agent.memory_context_recent_utterances,
                retrieval_limit=CFG.agent.memory_retrieval_max_items,
            )
        ready_artifact_count = sum(
            1
            for artifact in self._memory.artifacts()
            if artifact.fetch_status == "ready" and artifact.content
        )
        log.info(
            "Reply context summary_mode=%s doc_route=%s context_chars=%d rolling_summary_chars=%d utterances=%d ready_artifacts=%d query=%r",
            summary_mode,
            doc_route,
            len(context),
            len(self._memory.rolling_summary),
            self._memory.utterance_count,
            ready_artifact_count,
            query,
        )
        return context, summary_mode, doc_route

    async def _handle_final(self, text: str, *, source: str) -> None:
        text = " ".join((text or "").strip().split())
        if not text:
            return
        key = _final_key(text)
        if source == "asr_final" and self._pending_soft_final_key == key:
            log.info("Ignoring duplicate ASR final after soft final: %s", text)
            self._pending_soft_final_key = None
            self._clear_partial_tracking()
            self._cancel_soft_final_task()
            return
        if source != "soft_final":
            self._pending_soft_final_key = None
        if source == "asr_final":
            self._mark_turn_activity(text)
        turn_latency_s = (
            time.monotonic() - self._turn_started_at if self._turn_started_at else 0.0
        )
        partial_age_s = (
            time.monotonic() - self._latest_partial_started_at
            if self._latest_partial_started_at
            else 0.0
        )
        self._clear_partial_tracking()
        self._cancel_soft_final_task()
        log.info(
            "Turn committed source=%s state=%s turn_latency=%.2fs partial_age=%.2fs text=%s",
            source,
            self._state.value,
            turn_latency_s,
            partial_age_s,
            text,
        )

        if self._state == State.WAITING:
            if not self._wake.is_wake(text):
                self._remember_transcript(text, source="meeting_passive_asr")
                self._turn_started_at = 0.0
                return
            await self._enter_engaged()
            query = self._wake.strip_wake(text).strip()
            if not query:
                await self._spawn_fixed_reply(self._wake_ack_text(text))
                self._turn_started_at = 0.0
                return
            if _is_low_value_query(query):
                log.info("Ignoring low-value wake query=%r", query)
                self._turn_started_at = 0.0
                return
            self._remember_transcript(query, source="user_query")
            await self._spawn_reply(query)
            return

        if self._state == State.SPEAKING:
            await self._abort_reply(next_state=State.ENGAGED)

        if self._end_session.is_stop(text):
            log.info("End-session intent detected -> WAITING")
            await self._enter_waiting()
            self._turn_started_at = 0.0
            return

        if self._stop.is_stop(text):
            log.info("STOP intent -> stay engaged silently")
            await self._enter_engaged()
            self._turn_started_at = 0.0
            return

        await self._enter_engaged()
        query = (
            self._wake.strip_wake(text).strip()
            if self._wake.is_wake(text)
            else text.strip()
        )
        if query:
            if _is_low_value_query(query):
                log.info("Ignoring low-value follow-up query=%r", query)
                self._turn_started_at = 0.0
                return
            self._remember_transcript(query, source="user_query")
            await self._spawn_reply(query)
            return
        self._turn_started_at = 0.0

    async def _on_asr_error(self, msg: str) -> None:
        log.warning("ASR error: %s", msg)
        if msg == "asr_send_failed" or msg == "asr_error:45000000":
            log.warning(
                "ASR session entered a non-recovering state -> closing realtime "
                "session so outer retry can rebuild"
            )
            self._shutdown_requested.set()
            self._cancel_soft_final_task()
            self._clear_partial_tracking()
            await self._rt.fail_recoverably("asr_session_failed")
            return
        if self._state == State.SPEAKING:
            log.warning("ASR failed during active reply -> abort reply")
            await self._abort_reply(next_state=State.ENGAGED)

    async def _on_meeting_event(self, event: MeetingEvent) -> None:
        updated = self._memory.add_meeting_event(event.event_key, event.payload)
        if updated:
            log.info(
                "Meeting event captured key=%s fields=%s",
                event.event_key,
                sorted(event.payload.keys()),
            )
            self._schedule_artifact_fetches()
        if event.event_key == "vc.meeting.participant_meeting_ended_v1":
            log.info("Current meeting ended -> graceful shutdown")
            self._exit_reason = "meeting_ended"
            self._shutdown_requested.set()
            self._cancel_soft_final_task()
            self._clear_partial_tracking()
            await self._enter_waiting()

    def _schedule_artifact_fetches(self) -> None:
        for artifact in self._memory.pending_artifacts():
            key = (artifact.kind, artifact.token)
            if key in self._artifact_tasks:
                continue
            if not self._memory.mark_artifact_fetching(artifact.kind, artifact.token):
                continue
            self._artifact_tasks[key] = asyncio.create_task(
                self._fetch_artifact_content(artifact.kind, artifact.token),
                name=f"artifact-fetch-{artifact.kind}",
            )

    async def _fetch_artifact_content(self, kind: str, token: str) -> None:
        key = (kind, token)
        try:
            artifact = next(
                item
                for item in self._memory.artifacts()
                if item.kind == kind and item.token == token
            )
            fetched = await fetch_artifact_content(artifact)
        except StopIteration:
            return
        except ArtifactFetchError as exc:
            self._memory.apply_artifact_error(kind, token, str(exc))
            log.warning(
                "Meeting artifact fetch failed kind=%s token=%s: %s", kind, token, exc
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._memory.apply_artifact_error(kind, token, str(exc))
            log.warning(
                "Meeting artifact fetch crashed kind=%s token=%s: %s", kind, token, exc
            )
            return
        finally:
            self._artifact_tasks.pop(key, None)
        if self._memory.apply_artifact_content(kind, token, fetched.content):
            log.info(
                "Meeting artifact content captured kind=%s token=%s chars=%d",
                kind,
                token,
                len(fetched.content),
            )

    def _remember_transcript(self, text: str, *, source: str) -> None:
        self._memory.add_transcript(text, source=source)
        self._maybe_schedule_memory_rollup()

    def _remember_assistant_reply(self, text: str) -> None:
        self._memory.add_transcript(text, source="assistant_reply")
        self._maybe_schedule_memory_rollup()

    def _maybe_schedule_memory_rollup(self) -> None:
        if not self._memory.needs_rollup(CFG.agent.memory_rollup_utterances):
            return
        if self._summary_task is not None and not self._summary_task.done():
            return
        self._summary_task = asyncio.create_task(
            self._run_memory_rollup(),
            name="meeting-memory-rollup",
        )

    async def _run_memory_rollup(self) -> None:
        transcript = self._memory.unsummarized_transcript(
            CFG.agent.memory_rollup_source_max_chars
        )
        if not transcript.strip():
            return
        summary = await self._llm.summarize_meeting_memory(
            self._memory.rolling_summary,
            transcript,
            max_chars=CFG.agent.memory_rollup_max_chars,
        )
        self._memory.apply_rolling_summary(
            summary,
            max_chars=CFG.agent.memory_rollup_max_chars,
        )

    async def _queue_pcm(self, pcm: bytes) -> None:
        for chunk in split_pcm(pcm):
            if not self._tts_audio_started:
                self._tts_audio_started = True
                self._tts_started_at = time.monotonic()
                turn_to_first_audio_s = (
                    self._tts_started_at - self._current_reply_turn_started_at
                    if self._current_reply_turn_started_at
                    else 0.0
                )
                log.info(
                    "TTS audio started — first_audio_latency=%.2fs turn_to_first_audio=%.2fs",
                    self._tts_started_at - self._reply_started_at,
                    turn_to_first_audio_s,
                )
            await self._sender.feed(chunk)

    async def _enter_engaged(self) -> None:
        self._engaged_last_active_at = time.monotonic()
        if self._state == State.WAITING:
            log.info("Conversation state WAITING -> ENGAGED")
        self._state = State.ENGAGED

    async def _enter_waiting(self) -> None:
        await self._abort_reply(next_state=State.WAITING)
        self._engaged_last_active_at = 0.0
        self._llm.reset()
        log.info("Conversation state -> WAITING")

    async def _maybe_expire_engaged_session(self) -> None:
        if CFG.agent.engaged_idle_timeout_s <= 0:
            return
        if self._state != State.ENGAGED or self._engaged_last_active_at <= 0:
            return
        if (
            time.monotonic() - self._engaged_last_active_at
            < CFG.agent.engaged_idle_timeout_s
        ):
            return
        log.info("Engaged session idle timeout reached -> WAITING")
        await self._enter_waiting()

    # ---------------- reply lifecycle ----------------

    async def _spawn_reply(self, query: str) -> None:
        async with self._state_lock:
            await self._abort_reply_locked(next_state=State.ENGAGED)
            self._cancel_soft_final_task()
            self._clear_partial_tracking()
            self._current_reply_turn_started_at = self._turn_started_at
            self._turn_started_at = 0.0
            self._cancel_event = asyncio.Event()
            self._tts_audio_started = False
            self._tts_started_at = 0.0
            self._reply_started_at = time.monotonic()
            self._reply_barge_in_count = 0
            self._engaged_last_active_at = time.monotonic()
            self._state = State.SPEAKING
            self._reply_task = asyncio.create_task(
                self._run_reply(query, self._cancel_event),
                name="reply",
            )

    async def _spawn_fixed_reply(self, text: str) -> None:
        async with self._state_lock:
            await self._abort_reply_locked(next_state=State.ENGAGED)
            self._cancel_soft_final_task()
            self._clear_partial_tracking()
            self._current_reply_turn_started_at = self._turn_started_at
            self._turn_started_at = 0.0
            self._cancel_event = asyncio.Event()
            self._tts_audio_started = False
            self._tts_started_at = 0.0
            self._reply_started_at = time.monotonic()
            self._reply_barge_in_count = 0
            self._engaged_last_active_at = time.monotonic()
            self._state = State.SPEAKING
            self._reply_task = asyncio.create_task(
                self._run_fixed_reply(text, self._cancel_event),
                name="fixed-reply",
            )

    async def _abort_reply(self, next_state: State = State.ENGAGED) -> None:
        async with self._state_lock:
            await self._abort_reply_locked(next_state=next_state)

    async def _abort_reply_locked(self, next_state: State = State.ENGAGED) -> None:
        if self._reply_task is None:
            self._tts_audio_started = False
            self._tts_started_at = 0.0
            self._state = next_state
            return
        self._cancel_event.set()
        # Drop queued audio + tell Feishu to wipe its play buffer.
        self._sender.drop_pending()
        try:
            await self._rt.send_clear()
        except Exception as e:  # noqa: BLE001
            log.warning("send_clear failed: %s", e)
        if not self._reply_task.done():
            self._reply_task.cancel()
            try:
                await self._reply_task
            except (asyncio.CancelledError, Exception):
                pass
        self._reply_task = None
        self._tts_audio_started = False
        self._tts_started_at = 0.0
        self._current_reply_turn_started_at = 0.0
        self._state = next_state

    async def _run_reply(self, query: str, cancel_event: asyncio.Event) -> None:
        log.info("Reply START query=%r", query)
        try:
            meeting_context, summary_mode, doc_route = self._build_meeting_context(
                query
            )
            log.info(
                "Reply mode summary_mode=%s doc_route=%s query=%r",
                summary_mode,
                doc_route,
                query,
            )
            effective_query = (
                canonicalize_doc_query(doc_route, query) if doc_route else query
            )
            reply_max_tokens = (
                CFG.llm.doc_route_max_tokens if doc_route else CFG.llm.max_tokens
            )
            token_stream = self._llm.stream(
                effective_query,
                cancel_event,
                meeting_context=meeting_context,
                max_tokens=reply_max_tokens,
                on_complete=self._remember_assistant_reply,
            )
            async for pcm in self._tts.synthesize_stream(token_stream, cancel_event):
                if cancel_event.is_set():
                    break
                await self._queue_pcm(pcm)
            if not cancel_event.is_set() and not self._tts_audio_started:
                log.warning("Reply produced no audio; speaking fallback prompt")
                async for pcm in self._tts.synthesize(
                    CFG.agent.reply_error_tts_text, cancel_event
                ):
                    if cancel_event.is_set():
                        break
                    await self._queue_pcm(pcm)
            if not cancel_event.is_set() and self._tts_audio_started:
                await self._sender.flush()
            total_s = (
                time.monotonic() - self._reply_started_at
                if self._reply_started_at
                else 0.0
            )
            log.info(
                "Reply DONE cancelled=%s total_latency=%.2fs barges=%d spoke_audio=%s",
                cancel_event.is_set(),
                total_s,
                self._reply_barge_in_count,
                self._tts_audio_started,
            )
        except asyncio.CancelledError:
            log.info("Reply task cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Reply task crashed: %s", e)
        finally:
            if self._state == State.SPEAKING and not cancel_event.is_set():
                async with self._state_lock:
                    self._tts_audio_started = False
                    self._tts_started_at = 0.0
                    self._engaged_last_active_at = time.monotonic()
                    self._state = State.ENGAGED
                    self._reply_task = None
                    self._reply_started_at = 0.0
                    self._reply_barge_in_count = 0

    async def _run_fixed_reply(self, text: str, cancel_event: asyncio.Event) -> None:
        log.info("Fixed reply START text=%r", text)
        try:
            async for pcm in self._tts.synthesize(text, cancel_event):
                if cancel_event.is_set():
                    break
                await self._queue_pcm(pcm)
            if not cancel_event.is_set() and self._tts_audio_started:
                await self._sender.flush()
            total_s = (
                time.monotonic() - self._reply_started_at
                if self._reply_started_at
                else 0.0
            )
            log.info(
                "Fixed reply DONE cancelled=%s total_latency=%.2fs spoke_audio=%s",
                cancel_event.is_set(),
                total_s,
                self._tts_audio_started,
            )
            if not cancel_event.is_set():
                self._remember_assistant_reply(text)
        except asyncio.CancelledError:
            log.info("Fixed reply task cancelled")
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("Fixed reply task crashed: %s", e)
        finally:
            if self._state == State.SPEAKING and not cancel_event.is_set():
                async with self._state_lock:
                    self._tts_audio_started = False
                    self._tts_started_at = 0.0
                    self._engaged_last_active_at = time.monotonic()
                    self._state = State.ENGAGED
                    self._reply_task = None
                    self._reply_started_at = 0.0
                    self._reply_barge_in_count = 0

    def _wake_ack_text(self, source_text: str) -> str:
        if _contains_cjk(source_text):
            return CFG.agent.wake_ack_tts_text_zh
        return CFG.agent.wake_ack_tts_text_en
