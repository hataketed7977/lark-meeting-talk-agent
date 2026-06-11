"""Entry point.

Three ways to start, in order of preference for path 2 (external integration):

    # (a) Another service already joined the meeting AND already fetched the WS URL.
    #     Nothing else to do — we just attach the audio session.
    python -m lark_meeting_voice --ws-url 'wss://...'

    # (b) Another service already joined; pass meeting_id, we fetch the WS URL.
    python -m lark_meeting_voice --meeting-id 7642440384966134751

    # (c) Standalone: this process performs bots/join itself.
    python -m lark_meeting_voice --meeting-no 123456789
"""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import inspect
import logging
import os
import signal
import sys

from lark_meeting_voice.agent.orchestrator import Orchestrator
from lark_meeting_voice.config import CFG
from lark_meeting_voice.lark.bot_join import (
    LarkAPIError,
    bot_join_meeting,
    bot_leave_meeting,
    get_realtime_endpoint,
)
from lark_meeting_voice.lark.realtime import RealtimeClient
from lark_meeting_voice.memory.meeting_memory import MeetingMemory


def _setup_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, CFG.agent.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    logging.getLogger("websockets.client").setLevel(logging.WARNING)
    logging.getLogger("websockets.protocol").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


def _install_parent_death_signal() -> None:
    """Linux only: when the parent process dies, send us SIGTERM.

    Prevents orphaned voice subprocesses if the parent process crashes / is killed,
    which would otherwise keep the Bot stuck in the meeting.
    """
    if sys.platform != "linux":
        return
    try:
        PR_SET_PDEATHSIG = 1  # noqa: N806
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0) != 0:
            err = ctypes.get_errno()
            logging.warning("prctl(PR_SET_PDEATHSIG) failed: errno=%s", err)
        else:
            logging.info("Parent-death signal installed (SIGTERM on parent exit)")
    except Exception as e:  # noqa: BLE001
        logging.warning("Could not install parent-death signal: %s", e)


async def _resolve_ws_url(
    *,
    ws_url: str | None,
    meeting_id: str | None,
    meeting_no: str | None,
) -> tuple[str, str | None]:
    if ws_url:
        logging.info("Using ws-url supplied by caller")
        return ws_url, meeting_id
    if meeting_id:
        logging.info(
            "Fetching realtime endpoint for existing meeting_id=%s", meeting_id
        )
        return await get_realtime_endpoint(meeting_id), meeting_id
    assert meeting_no, "must provide one of --ws-url / --meeting-id / --meeting-no"
    mid, _ = await bot_join_meeting(meeting_no)
    return await get_realtime_endpoint(mid), mid


async def _run(
    *,
    ws_url: str | None,
    meeting_id: str | None,
    meeting_no: str | None,
) -> int:
    CFG.validate(
        require_feishu_access=not bool(ws_url),
        require_feishu_user_token=bool(meeting_id or meeting_no),
    )

    stop_evt = asyncio.Event()

    def _stop(*_args):
        logging.info("Signal received, shutting down")
        stop_evt.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _stop())

    max_attempts = max(1, CFG.agent.reconnect_attempts)
    backoff_s = max(0.0, CFG.agent.reconnect_backoff_s)
    recoverable_leave_delays = {
        "stale_stream_publish_session": 1.5,
        "stream_agent_cooldown": 4.0,
        "asr_session_failed": 2.0,
        "startup_silent_downstream": 1.5,
    }
    retained_memory: MeetingMemory | None = None
    retained_meeting_id: str | None = None

    for attempt in range(1, max_attempts + 1):
        rt: RealtimeClient | None = None
        orch: Orchestrator | None = None
        orch_task: asyncio.Task | None = None
        current_meeting_id: str | None = None
        recoverable: str | None = None
        should_retry = False
        try:
            resolved_ws_url, current_meeting_id = await _resolve_ws_url(
                ws_url=ws_url,
                meeting_id=meeting_id,
                meeting_no=meeting_no,
            )
            rt = RealtimeClient(resolved_ws_url)
            await rt.connect()
            reuse_memory = (
                retained_memory
                if retained_memory is not None
                and retained_meeting_id
                and retained_meeting_id == current_meeting_id
                else None
            )
            orch_kwargs = {"meeting_id": current_meeting_id}
            if "memory" in inspect.signature(Orchestrator).parameters:
                orch_kwargs["memory"] = reuse_memory
            orch = Orchestrator(rt, **orch_kwargs)
            await rt.wait_session_created()
            logging.info("Realtime session ready; starting audio upstream")
            await orch.start_realtime_audio()
            logging.info("Realtime audio upstream ready; entering orchestrator loop")
            orch_task = asyncio.create_task(orch.run(), name=f"orchestrator-{attempt}")
            stop_task = asyncio.create_task(
                stop_evt.wait(), name=f"stop-wait-{attempt}"
            )

            done, pending = await asyncio.wait(
                {orch_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()

            if stop_task in done:
                logging.info("Stop requested; exiting")
                return 0

            exc = orch_task.exception()
            if exc is not None:
                logging.error(
                    "Orchestrator crashed on attempt %d/%d: %r",
                    attempt,
                    max_attempts,
                    exc,
                    exc_info=exc,
                )
            elif orch is not None and orch.exit_reason == "meeting_ended":
                logging.info("Meeting ended; exiting without retry")
                return 0
            else:
                recoverable = rt.recoverable_fatal_error if rt is not None else None
                if recoverable:
                    should_retry = recoverable in recoverable_leave_delays
                    logging.warning(
                        "Realtime session ended due to recoverable error=%s "
                        "on attempt %d/%d",
                        recoverable,
                        attempt,
                        max_attempts,
                    )
                else:
                    logging.error(
                        "Realtime session ended unexpectedly on attempt %d/%d; "
                        "not auto-retrying to avoid churn",
                        attempt,
                        max_attempts,
                    )
        except Exception as exc:  # noqa: BLE001
            logging.error(
                "Realtime session setup failed on attempt %d/%d: %r",
                attempt,
                max_attempts,
                exc,
                exc_info=exc,
            )
            if isinstance(exc, LarkAPIError) and not exc.retryable:
                logging.error(
                    "Non-retryable Lark API error code=%s; stop retrying", exc.code
                )
                return 1
            should_retry = True
            logging.warning(
                "Setup failure is treated as transient; retrying if attempts remain"
            )
        finally:
            if rt is not None:
                await rt.close(reason="USER_LEFT")
            if orch_task is not None:
                orch_task.cancel()
                try:
                    await orch_task
                except (asyncio.CancelledError, Exception):
                    pass
            if recoverable in recoverable_leave_delays and orch is not None:
                retained_memory = orch.memory
                retained_meeting_id = current_meeting_id
            elif recoverable is None:
                retained_memory = None
                retained_meeting_id = None
            if (
                recoverable in recoverable_leave_delays
                and meeting_no
                and current_meeting_id
            ):
                logging.warning(
                    "Forcing bot leave after recoverable error=%s before retry: "
                    "meeting_id=%s",
                    recoverable,
                    current_meeting_id,
                )
                try:
                    await bot_leave_meeting(current_meeting_id)
                except Exception as exc:  # noqa: BLE001
                    logging.warning("Forced bot leave failed: %s", exc)
                else:
                    await asyncio.sleep(recoverable_leave_delays[recoverable])

        if stop_evt.is_set():
            break
        if not should_retry:
            return 1
        if attempt >= max_attempts:
            break

        delay = backoff_s * (2 ** (attempt - 1))
        logging.warning(
            "Retrying realtime session in %.1fs (attempt %d/%d)",
            delay,
            attempt + 1,
            max_attempts,
        )
        await asyncio.sleep(delay)

    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Lark Meeting Talk Agent — realtime Feishu meeting voice assistant"
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--ws-url",
        help="Realtime WebSocket URL returned by /vc/v1/realtime/endpoint. "
        "Pass this when another service has already fetched it — skips one HTTP call.",
    )
    grp.add_argument(
        "--meeting-id",
        help="Already-joined meeting id (another service did the bots/join). "
        "We will call realtime/endpoint ourselves to get the WS URL.",
    )
    grp.add_argument(
        "--meeting-no",
        help="Numeric meeting number. Use only if you want this process to do "
        "bots/join itself (standalone / smoke-test mode).",
    )
    args = parser.parse_args()
    _setup_logging()
    _install_parent_death_signal()
    return asyncio.run(
        _run(
            ws_url=args.ws_url,
            meeting_id=args.meeting_id,
            meeting_no=args.meeting_no,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
