"""Doubao streaming LLM client (OpenAI-compatible Ark endpoint)."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, List

from openai import AsyncOpenAI

from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)


class DoubaoLLM:
    def __init__(self) -> None:
        self._client = AsyncOpenAI(
            api_key=CFG.llm.api_key,
            base_url=CFG.llm.base_url,
        )
        self._history: List[dict] = []
        self._max_turns = CFG.llm.max_history_turns

    def reset(self) -> None:
        self._history.clear()

    async def summarize_meeting_memory(
        self,
        previous_summary: str,
        transcript_delta: str,
        *,
        max_chars: int,
    ) -> str:
        """Update the in-memory rolling summary without touching chat history."""
        if not transcript_delta.strip():
            return previous_summary

        prompt = (
            "Update the rolling summary of a live meeting. Keep it compact and useful "
            "for later global questions such as 'summarize/evaluate this sharing'. "
            "Preserve concrete facts and organize around topics, key points, decisions, "
            "risks, open questions, action items, and presentation-quality signals. "
            "Do not invent details.\n\n"
            f"Previous summary:\n{previous_summary or '- None yet.'}\n\n"
            f"New transcript:\n{transcript_delta}\n\n"
            f"Return an updated summary under {max_chars} characters."
        )
        started_at = time.monotonic()
        try:
            response = await self._client.chat.completions.create(
                model=CFG.llm.model,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You maintain compact structured memory for a live meeting "
                            "assistant. Be factual, compressed, and useful for later Q&A."
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                stream=False,
                temperature=0.2,
                max_tokens=260,
                timeout=CFG.llm.request_timeout_s,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("Meeting memory rollup failed: %s", e)
            return previous_summary

        text = (response.choices[0].message.content or "").strip()
        log.info(
            "Meeting memory rollup updated chars=%d latency=%.2fs",
            len(text),
            time.monotonic() - started_at,
        )
        return text or previous_summary

    def _messages(
        self, user_text: str, meeting_context: str | None = None
    ) -> List[dict]:
        msgs: List[dict] = [{"role": "system", "content": CFG.llm.system_prompt}]
        if meeting_context:
            trimmed = meeting_context[-CFG.llm.meeting_context_max_chars :].strip()
            if trimmed:
                msgs.append(
                    {
                        "role": "system",
                        "content": (
                            "Meeting Memory is the source of truth. Use Rolling summary for "
                            "global questions, Relevant earlier transcript for evidence, and "
                            "Recent transcript for immediate context. Do not guess beyond the "
                            "memory. Keep the first spoken sentence short so TTS can start "
                            "quickly.\n\n"
                            f"{trimmed}"
                        ),
                    }
                )
        msgs.extend(self._history[-2 * self._max_turns :])
        msgs.append({"role": "user", "content": user_text})
        return msgs

    async def stream(
        self,
        user_text: str,
        cancel_event: asyncio.Event,
        meeting_context: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream assistant tokens. Yields text deltas.

        Updates history with the full reply on natural completion.
        Stops cleanly if cancel_event is set.
        """
        messages = self._messages(user_text, meeting_context=meeting_context)
        full_reply = []
        started_at = time.monotonic()
        first_token_logged = False
        prompt_chars = sum(len(str(msg.get("content", ""))) for msg in messages)
        try:
            stream = await self._client.chat.completions.create(
                model=CFG.llm.model,
                messages=messages,
                stream=True,
                temperature=0.6,
                max_tokens=CFG.llm.max_tokens,
                timeout=CFG.llm.request_timeout_s,
            )
            log.info(
                "LLM stream created prompt_chars=%d max_tokens=%d setup_latency=%.2fs",
                prompt_chars,
                CFG.llm.max_tokens,
                time.monotonic() - started_at,
            )
            stream_iter = stream.__aiter__()
            while True:
                if cancel_event.is_set():
                    log.info("LLM cancelled mid-stream")
                    try:
                        await stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                try:
                    chunk = await asyncio.wait_for(
                        stream_iter.__anext__(),
                        timeout=CFG.llm.stream_idle_timeout_s,
                    )
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    log.warning(
                        "LLM stream idle timeout after %.1fs",
                        CFG.llm.stream_idle_timeout_s,
                    )
                    try:
                        await stream.close()
                    except Exception:  # noqa: BLE001
                        pass
                    break
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                token = delta.content or ""
                if token:
                    if not first_token_logged:
                        first_token_logged = True
                        log.info(
                            "LLM first token latency=%.2fs",
                            time.monotonic() - started_at,
                        )
                    full_reply.append(token)
                    yield token
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("LLM stream error: %s", e)
            return
        finally:
            text = "".join(full_reply).strip()
            if text and not cancel_event.is_set():
                self._history.append({"role": "user", "content": user_text})
                self._history.append({"role": "assistant", "content": text})


# Helper: split a token stream into sentence-sized chunks for TTS pacing.
SENTENCE_TERMINATORS = set("。.!?！？\n")


async def sentence_chunks(
    token_stream: AsyncIterator[str],
    cancel_event: asyncio.Event,
    min_chars: int = 6,
    max_chars: int = 80,
) -> AsyncIterator[str]:
    buf: List[str] = []
    async for tok in token_stream:
        if cancel_event.is_set():
            break
        buf.append(tok)
        joined = "".join(buf)
        if (
            any(c in SENTENCE_TERMINATORS for c in tok)
            and len(joined.strip()) >= min_chars
        ):
            yield joined.strip()
            buf.clear()
        elif len(joined.strip()) >= max_chars:
            yield joined.strip()
            buf.clear()
    if buf and not cancel_event.is_set():
        tail = "".join(buf).strip()
        if tail:
            yield tail
