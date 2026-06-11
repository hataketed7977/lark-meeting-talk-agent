import asyncio
import time

from lark_meeting_voice import main as main_mod
from lark_meeting_voice.agent.orchestrator import (
    Orchestrator,
    State,
    _is_low_value_query,
    _is_summary_query,
)
from lark_meeting_voice.config import CFG
from lark_meeting_voice.knowledge_routes import (
    build_doc_context,
    canonicalize_doc_query,
    match_doc_route,
)
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


def test_is_summary_query_detects_global_summary_questions():
    assert _is_summary_query("summarize this meeting")
    assert _is_summary_query("what are the action items")
    assert _is_summary_query("how was this sharing")
    assert _is_summary_query("总结一下这个会议")
    assert not _is_summary_query("what time is it")


def test_doc_route_matches_lark_cli_queries():
    assert match_doc_route("introduce lark-cli") == "lark-cli"
    assert match_doc_route("介绍一下 Lark CLI") == "lark-cli"
    assert match_doc_route("介绍一下 Lark") == "lark-cli"
    assert match_doc_route("this l a r k c l i") == "lark-cli"
    assert match_doc_route("介绍一下 luck cli") == "lark-cli"
    assert match_doc_route("what is luck cli") == "lark-cli"
    assert match_doc_route("我正在介绍 lark cli，你能帮助我吗") == "lark-cli"
    assert match_doc_route("介绍一下这个 CLI") is None
    assert (
        match_doc_route("actually, i'm to introduce about luxcli can you help me")
        is None
    )
    assert match_doc_route("what time is it") is None


def test_doc_context_loads_lark_cli_note():
    block = build_doc_context("lark-cli", max_chars=2200)
    assert "Reference note: Lark CLI" in block
    assert "official open-source CLI" in block


def test_doc_context_for_live_presentation_requests_cohesive_intro():
    block = build_doc_context(
        "lark-cli",
        query="我正在介绍 lark cli，你能帮助我吗",
        max_chars=2200,
    )
    assert "cohesive introduction" in block
    assert "what it is, what it does, why it matters" in block
    assert "Do not center the answer on this meeting project" in block
    assert "four to six sentences" in block


def test_doc_query_is_canonicalized_for_asr_cli_variants():
    rewritten = canonicalize_doc_query(
        "lark-cli",
        "actually, i'm introducing luck cli, can you help me",
    )
    assert "Lark CLI" in rewritten
    assert "luck cli" not in rewritten.lower()
    assert "official open-source CLI" in rewritten
    assert "Focus on Lark CLI itself" in rewritten


def test_doc_query_does_not_rewrite_unrelated_cli_brand():
    rewritten = canonicalize_doc_query(
        "lark-cli",
        "actually, i'm to introduce about luxcli can you help me",
    )
    assert "luxcli" in rewritten.lower()
    assert "Lark CLI" in rewritten


def test_doc_markdown_uses_word_numbers_for_spoken_intro():
    note = build_doc_context("lark-cli", query="introduce lark cli", max_chars=2600)
    assert "eighteen business domains" in note
    assert "two hundred curated commands" in note
    assert "twenty built-in agent skills" in note


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


def test_orchestrator_escalates_fatal_asr_errors_to_recoverable_restart(monkeypatch):
    async def _run() -> None:
        rt = RealtimeClient("wss://example.invalid/realtime")
        orchestrator = Orchestrator(rt)
        calls: list[str] = []

        async def fake_fail_recoverably(kind: str) -> None:
            calls.append(kind)

        monkeypatch.setattr(rt, "fail_recoverably", fake_fail_recoverably)

        await orchestrator._on_asr_error("asr_error:45000000")

        assert calls == ["asr_session_failed"]
        assert orchestrator._shutdown_requested.is_set()

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


def test_meeting_memory_builds_summary_context_without_recent_transcript_noise():
    memory = MeetingMemory(max_recent_utterances=8)
    memory.add_transcript("We agreed to ship the event pipeline this week.")
    memory.add_transcript("Alice owns the rollout checklist by Friday.")
    memory.add_transcript("There is a risk around permissions in external tenants.")
    memory.add_transcript(
        "This is a very recent side conversation that should not dominate."
    )
    memory.apply_rolling_summary(
        "The team reviewed the launch plan, agreed to ship this week, and identified "
        "external-tenant permissions as the main risk.",
        max_chars=400,
    )
    memory.add_meeting_event(
        "minutes.minute.generated_v1",
        {
            "event_id": "evt-minute-1",
            "title": "Weekly Sync",
            "minute_token": "minute-1",
            "minute_source": {"source_entity_id": "meeting-1"},
        },
    )
    memory.apply_artifact_content(
        "minute",
        "minute-1",
        "Decision: ship the event pipeline this week. Action item: Alice sends the "
        "rollout checklist by Friday.",
    )

    block = memory.build_summary_context_block(
        "summarize this meeting",
        max_chars=900,
        summary_max_chars=280,
        facts_max_chars=260,
        artifact_max_chars=220,
        retrieval_limit=2,
    )

    assert "Rolling summary:" in block
    assert "Action items:" in block
    assert "Current meeting artifact excerpts:" in block
    assert "Recent transcript:" not in block


def test_orchestrator_uses_summary_context_for_summary_query(monkeypatch):
    rt = RealtimeClient("wss://example.invalid/realtime")
    orchestrator = Orchestrator(rt)
    calls: list[tuple[str, object]] = []

    def fake_summary_context(*_args, **kwargs):
        calls.append(("summary", kwargs.get("max_chars")))
        return "summary-context"

    def fake_normal_context(*_args, **kwargs):
        calls.append(("normal", kwargs.get("recent_limit")))
        return "normal-context"

    monkeypatch.setattr(
        "lark_meeting_voice.agent.orchestrator.match_doc_route",
        lambda _query: None,
    )
    orchestrator._memory.build_summary_context_block = fake_summary_context  # type: ignore[method-assign]
    orchestrator._memory.build_context_block = fake_normal_context  # type: ignore[method-assign]

    summary_context, summary_mode, summary_doc_route = (
        orchestrator._build_meeting_context("summarize this meeting")
    )
    normal_context, normal_mode, normal_doc_route = orchestrator._build_meeting_context(
        "what time is it"
    )

    assert summary_mode is True
    assert summary_doc_route is None
    assert summary_context == "summary-context"
    assert normal_mode is False
    assert normal_doc_route is None
    assert normal_context == "normal-context"
    assert calls == [
        ("summary", CFG.agent.summary_context_max_chars),
        ("normal", CFG.agent.memory_context_recent_utterances),
    ]


def test_orchestrator_uses_doc_context_for_matched_query(monkeypatch):
    rt = RealtimeClient("wss://example.invalid/realtime")
    orchestrator = Orchestrator(rt)

    monkeypatch.setattr(
        "lark_meeting_voice.agent.orchestrator.match_doc_route",
        lambda _query: "lark-cli",
    )
    monkeypatch.setattr(
        "lark_meeting_voice.agent.orchestrator.build_doc_context",
        lambda route_key, *, query, max_chars: f"doc:{route_key}:{query}:{max_chars}",
    )

    context, summary_mode, doc_route = orchestrator._build_meeting_context(
        "introduce lark-cli"
    )

    assert (
        context == f"doc:lark-cli:introduce lark-cli:{CFG.agent.doc_context_max_chars}"
    )
    assert summary_mode is False
    assert doc_route == "lark-cli"


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
                _ = reason
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


def test_main_reuses_meeting_memory_across_recoverable_retry(monkeypatch):
    async def _run() -> None:
        attempts = 0
        memories: list[object | None] = []

        async def fake_resolve_ws_url(**_kwargs) -> tuple[str, str]:
            nonlocal attempts
            attempts += 1
            return "wss://example.invalid/realtime", "meeting-1"

        class DummyRealtimeClient:
            instance_count = 0

            def __init__(self, _ws_url: str) -> None:
                type(self).instance_count += 1
                self._closed = asyncio.Event()
                self.recoverable_fatal_error = (
                    "stream_agent_cooldown" if type(self).instance_count == 1 else None
                )

            async def connect(self) -> None:
                return None

            async def wait_session_created(self) -> None:
                return None

            async def close(self, reason: str = "USER_LEFT") -> None:
                _ = reason
                self._closed.set()

        class DummyOrchestrator:
            def __init__(
                self,
                _rt,
                *,
                meeting_id: str | None = None,
                memory=None,
            ) -> None:
                self.meeting_id = meeting_id
                self.exit_reason = None
                self.memory = memory or MeetingMemory()
                memories.append(memory)

            async def run(self) -> None:
                self.memory.add_transcript("persist me", source="user_query")
                return None

        async def fake_leave(_meeting_id: str) -> None:
            return None

        monkeypatch.setattr(CFG, "validate", lambda **_kwargs: None)
        monkeypatch.setattr(main_mod, "_resolve_ws_url", fake_resolve_ws_url)
        monkeypatch.setattr(main_mod, "RealtimeClient", DummyRealtimeClient)
        monkeypatch.setattr(main_mod, "Orchestrator", DummyOrchestrator)
        monkeypatch.setattr(main_mod, "bot_leave_meeting", fake_leave)
        monkeypatch.setattr(CFG.agent, "reconnect_attempts", 2)
        monkeypatch.setattr(CFG.agent, "reconnect_backoff_s", 0.01)

        result = await main_mod._run(
            ws_url=None,
            meeting_id=None,
            meeting_no="616633662",
        )

        assert result == 1
        assert attempts == 2
        assert memories[0] is None
        assert memories[1] is not None
        assert len(memories) == 2
        assert memories[1].utterance_count == 2

    asyncio.run(_run())


def test_main_retries_transient_setup_failures(monkeypatch):
    async def _run() -> None:
        attempts = 0

        async def fake_resolve_ws_url(**_kwargs) -> tuple[str, str]:
            return "wss://example.invalid/realtime", "meeting-1"

        class DummyRealtimeClient:
            def __init__(self, _ws_url: str) -> None:
                self._closed = asyncio.Event()
                self.recoverable_fatal_error = None

            async def connect(self) -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("temporary connect failure")

            async def wait_session_created(self) -> None:
                return None

            async def close(self, reason: str = "USER_LEFT") -> None:
                _ = reason
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
        monkeypatch.setattr(CFG.agent, "reconnect_attempts", 2)
        monkeypatch.setattr(CFG.agent, "reconnect_backoff_s", 0.01)

        result = await main_mod._run(
            ws_url=None,
            meeting_id=None,
            meeting_no="616633662",
        )

        assert result == 0
        assert attempts == 2

    asyncio.run(_run())
