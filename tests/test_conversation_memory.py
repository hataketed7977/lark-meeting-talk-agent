import asyncio
import time

from lark_meeting_voice.agent.orchestrator import (
    Orchestrator,
    State,
    _is_low_value_query,
)
from lark_meeting_voice.config import CFG
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
