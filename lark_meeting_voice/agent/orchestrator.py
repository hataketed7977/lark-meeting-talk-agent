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
import struct
import time
from typing import Optional

from lark_meeting_voice.asr.volc_asr import VolcASR
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
from lark_meeting_voice.llm.openai_compatible import OpenAICompatibleLLM, sentence_chunks
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
    ) -> None:
        self._rt = realtime
        self._meeting_id = meeting_id
        self._wake = WakeDetector(CFG.agent.wake_words)
        self._stop = StopClassifier(CFG.agent.stop_words)
        self._end_session = StopClassifier(CFG.agent.end_session_words)
        self._llm = OpenAICompatibleLLM()
        self._tts = VolcTTS()
        self._memory = MeetingMemory(
            max_recent_utterances=CFG.agent.memory_recent_utterances,
            max_summary_items=CFG.agent.memory_summary_items,
        )
        self._asr: Optional[VolcASR] = None
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

    async def run(self) -> None:
        if self._meeting_id:
            self._event_consumers = CurrentMeetingEventConsumers(
                self._meeting_id,
                self._on_meeting_event,
            )
            await self._event_consumers.start()
            log.info("Meeting event consumers started meeting_id=%s", self._meeting_id)
        # Start the paced sender FIRST so we begin streaming silence frames
        # upstream within ~100ms of session.created. The Feishu Realtime
        # server closes the session (reason=0) after ~1s of no upstream audio,
        # and ASR start() does a network handshake that can take several
        # hundred ms.
        try:
            await self._sender.start()
            log.info("PacedSender started")
        except Exception:
            log.exception("Failed to start PacedSender — aborting")
            raise

        self._asr = VolcASR(
            on_partial=self._on_partial,
            on_final=self._on_final,
            on_error=self._on_asr_error,
        )
        try:
            await self._asr.start()
            log.info("VolcASR started")
        except Exception:
            log.exception("Failed to start VolcASR — aborting orchestrator")
            raise

        # Spawn the downstream pump (Feishu -> ASR).
        pump_task = asyncio.create_task(self._downstream_pump(), name="downstream-pump")

        try:
            while not self._rt._closed.is_set():  # type: ignore[attr-defined]
                await asyncio.sleep(0.5)
                await self._maybe_expire_engaged_session()
        finally:
            pump_task.cancel()
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
        try:
            async for audio in self._rt.downstream():
                if not audio.pcm:
                    continue
                chunk_count += 1
                if chunk_count == 1 or chunk_count % 100 == 0:
                    peak24k, rms24k = _pcm16_metrics(audio.pcm)
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
        log.debug("ASR partial: %s", text)
        if self._state != State.SPEAKING:
            return
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
        log.info("ASR final: %s", text)

        if self._state == State.WAITING:
            if not self._wake.is_wake(text):
                return
            await self._enter_engaged()
            query = self._wake.strip_wake(text).strip()
            if not query:
                await self._spawn_fixed_reply(self._wake_ack_text(text))
                return
            await self._spawn_reply(query)
            return

        if self._state == State.SPEAKING:
            await self._abort_reply(next_state=State.ENGAGED)

        if self._end_session.is_stop(text):
            log.info("End-session intent detected -> WAITING")
            await self._enter_waiting()
            return

        if self._stop.is_stop(text):
            log.info("STOP intent -> stay engaged silently")
            await self._enter_engaged()
            return

        await self._enter_engaged()
        query = (
            self._wake.strip_wake(text).strip()
            if self._wake.is_wake(text)
            else text.strip()
        )
        if query:
            await self._spawn_reply(query)

    async def _on_asr_error(self, msg: str) -> None:
        log.warning("ASR error: %s", msg)
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
            log.warning("Meeting artifact fetch failed kind=%s token=%s: %s", kind, token, exc)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self._memory.apply_artifact_error(kind, token, str(exc))
            log.warning("Meeting artifact fetch crashed kind=%s token=%s: %s", kind, token, exc)
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
                log.info(
                    "TTS audio started — first_audio_latency=%.2fs",
                    self._tts_started_at - self._reply_started_at,
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
        self._state = next_state

    async def _run_reply(self, query: str, cancel_event: asyncio.Event) -> None:
        log.info("Reply START query=%r", query)
        try:
            token_stream = self._llm.stream(
                query,
                cancel_event,
                meeting_context=self._memory.build_context_block(
                    query,
                    recent_limit=CFG.agent.memory_context_recent_utterances,
                    retrieval_limit=CFG.agent.memory_retrieval_max_items,
                ),
            )
            async for sentence in sentence_chunks(
                token_stream,
                cancel_event,
                min_chars=CFG.llm.tts_chunk_min_chars,
                max_chars=CFG.llm.tts_chunk_max_chars,
            ):
                if cancel_event.is_set():
                    break
                log.debug("TTS sentence: %s", sentence)
                async for pcm in self._tts.synthesize(sentence, cancel_event):
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
