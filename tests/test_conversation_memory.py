import asyncio
import time

from lark_meeting_voice import main as main_mod
from lark_meeting_voice.agent.orchestrator import (
    Orchestrator,
    State,
    _is_low_value_query,
)
from lark_meeting_voice.config import CFG
from lark_meeting_voice.lark.event_consumer import MeetingEvent
from lark_meeting_voice.lark.realtime import RealtimeClient
from lark_meeting_voice.memory.meeting_memory import MeetingMemory


def test_meeting_memory_includes_recent_conversation_transcripts():
    memory = MeetingMemory(max_recent_utterances=6)
    memory.add_transcript("Can you hear me now?", source="user_query")
    memory.add_transcript("Yes, I can hear you clearly.", source="assistant_reply")

    block = memory.build_context_block("what did we just say")

    assert "- Total captured utterances: 2" in block
    assert "Recent transcript:" in block
    assert "- Can you hear me now?" in block
    assert "- Yes, I can hear you clearly." in block


def test_orchestrator_keeps_engaged_state_when_idle_timeout_disabled(monkeypatch):
    async def _run() -> None:
        rt = RealtimeClient("wss://example.invalid/realtime")
        orchestrator = Orchestrator(rt)
        monkeypatch.setattr(CFG.agent, "engaged_idle_timeout_s", 0)
        orchestrator._state = State.ENGAGED
        orchestrator._engaged_last_active_at = time.monotonic() - 3600

        await orchestrator._maybe_expire_engaged_session()

        assert orchestrator._state == State.ENGAGED

    asyncio.run(_run())


def test_is_low_value_query_only_filters_explicit_fillers():
    assert _is_low_value_query("ooh")
    assert _is_low_value_query("uh...")
    assert _is_low_value_query("Hmm!")
    assert not _is_low_value_query("yes")
    assert not _is_low_value_query("no")
    assert not _is_low_value_query("what time is it")
    assert not _is_low_value_query("帮我总结一下")
    assert not _is_low_value_query("James，帮我总结一下")


def test_orchestrator_meeting_ended_sets_graceful_shutdown():
    async def _run() -> None:
        rt = RealtimeClient("wss://example.invalid/realtime")
        orchestrator = Orchestrator(rt, meeting_id="meeting-1")

        await orchestrator._on_meeting_event(
            MeetingEvent(
                event_key="vc.meeting.participant_meeting_ended_v1",
                payload={
                    "event_id": "evt-end-1",
                    "meeting_id": "meeting-1",
                    "meeting_no": "616633662",
                    "topic": "Weekly Sync",
                    "end_time": "2026-06-11T16:19:35+08:00",
                },
            )
        )

        assert orchestrator.exit_reason == "meeting_ended"
        assert orchestrator._shutdown_requested.is_set()
        assert orchestrator._state == State.WAITING

    asyncio.run(_run())


def test_orchestrator_soft_final_promotes_stable_partial_without_duplicate_reply(
    monkeypatch,
):
    async def _run() -> None:
        rt = RealtimeClient("wss://example.invalid/realtime")
        orchestrator = Orchestrator(rt)
        replies: list[str] = []

        monkeypatch.setattr(CFG.agent, "asr_soft_final_quiet_window_s", 0.01)
        monkeypatch.setattr(CFG.agent, "asr_soft_final_min_chars", 8)

        async def fake_spawn_reply(query: str) -> None:
            replies.append(query)

        monkeypatch.setattr(orchestrator, "_spawn_reply", fake_spawn_reply)
        orchestrator._state = State.ENGAGED

        await orchestrator._on_partial("what are the next steps")
        await asyncio.sleep(0.03)
        await orchestrator._on_final("what are the next steps")

        assert replies == ["what are the next steps"]

    asyncio.run(_run())


def test_orchestrator_engaged_soft_final_uses_faster_follow_up_policy(monkeypatch):
    async def _run() -> None:
        rt = RealtimeClient("wss://example.invalid/realtime")
        orchestrator = Orchestrator(rt)
        replies: list[str] = []

        monkeypatch.setattr(CFG.agent, "asr_soft_final_quiet_window_s", 0.2)
        monkeypatch.setattr(CFG.agent, "asr_soft_final_min_chars", 8)
        monkeypatch.setattr(CFG.agent, "engaged_asr_soft_final_quiet_window_s", 0.01)
        monkeypatch.setattr(CFG.agent, "engaged_asr_soft_final_min_chars", 6)

        async def fake_spawn_reply(query: str) -> None:
            replies.append(query)

        monkeypatch.setattr(orchestrator, "_spawn_reply", fake_spawn_reply)
        orchestrator._state = State.ENGAGED

        await orchestrator._on_partial("next steps")
        await asyncio.sleep(0.03)

        assert replies == ["next steps"]

    asyncio.run(_run())


def test_main_exits_without_retry_after_meeting_end(monkeypatch):
    async def _run() -> None:
        attempts = 0

        async def fake_resolve_ws_url(**_kwargs) -> tuple[str, str]:
            nonlocal attempts
            attempts += 1
            return "wss://example.invalid/realtime", "meeting-1"

        class DummyRealtimeClient:
            def __init__(self, _ws_url: str) -> None:
                self._closed = asyncio.Event()
                self.recoverable_fatal_error = None

            async def connect(self) -> None:
                return None

            async def wait_session_created(self) -> None:
                return None

            async def close(self, reason: str = "USER_LEFT") -> None:
                self._closed.set()

        class DummyOrchestrator:
            def __init__(self, _rt, *, meeting_id: str | None = None) -> None:
                self.meeting_id = meeting_id
                self.exit_reason = "meeting_ended"

            async def run(self) -> None:
                return None

        monkeypatch.setattr(CFG, "validate", lambda **_kwargs: None)
        monkeypatch.setattr(main_mod, "_resolve_ws_url", fake_resolve_ws_url)
        monkeypatch.setattr(main_mod, "RealtimeClient", DummyRealtimeClient)
        monkeypatch.setattr(main_mod, "Orchestrator", DummyOrchestrator)
        monkeypatch.setattr(CFG.agent, "reconnect_attempts", 3)
        monkeypatch.setattr(CFG.agent, "reconnect_backoff_s", 0.01)

        result = await main_mod._run(
            ws_url=None,
            meeting_id=None,
            meeting_no="616633662",
        )

        assert result == 0
        assert attempts == 1

    asyncio.run(_run())
