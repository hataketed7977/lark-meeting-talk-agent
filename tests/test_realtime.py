import asyncio

from lark_meeting_voice._pb import meeting_realtime_pb2 as mr
from lark_meeting_voice.lark.realtime import RealtimeClient


def test_stale_publish_conflict_triggers_recoverable_close():
    async def _run() -> None:
        client = RealtimeClient("wss://example.invalid/realtime")
        closed = {}

        async def fake_close(reason: str = "USER_LEFT") -> None:
            closed["reason"] = reason
            client._closed.set()  # type: ignore[attr-defined]

        client.close = fake_close  # type: ignore[method-assign]

        ev = mr.ServerEvent(
            type="error",
            event_id="evt-1",
            session_id=123,
            error=mr.Error(
                client_event_id="client-1",
                code=1001,
                message=(
                    "worker error: code=subscribe_rejected "
                    "msg=upsert vcc agent stream runtime: "
                    "stale stream publish session, current=1, incoming=2"
                ),
                retryable=False,
            ),
        )

        client._dispatch(ev)
        await asyncio.sleep(0)

        assert client.recoverable_fatal_error == "stale_stream_publish_session"
        assert closed == {"reason": "CLIENT_ERROR"}

    asyncio.run(_run())
