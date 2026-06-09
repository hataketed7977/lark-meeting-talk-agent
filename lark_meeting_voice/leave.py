"""One-shot CLI to make the bot leave a Feishu meeting.

Usage:
    # By meeting_id (returned from /bots/join)
    python -m lark_meeting_voice.leave --meeting-id 7648900646066965732

    # By meeting_no — we'll resolve meeting_id via /bots/join (rejoins first,
    # then leaves; mainly useful when you only have the human-facing number).
    python -m lark_meeting_voice.leave --meeting-no 616633662

Reads FEISHU_USER_ACCESS_TOKEN (etc.) from .env, same as the main agent.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from lark_meeting_voice.config import CFG
from lark_meeting_voice.lark.bot_join import (
    bot_join_meeting,
    bot_leave_meeting,
)


async def _run(meeting_id: str | None, meeting_no: str | None) -> int:
    CFG.validate()
    if not meeting_id:
        assert meeting_no
        meeting_id, _ = await bot_join_meeting(meeting_no)
    await bot_leave_meeting(meeting_id)
    return 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Make the bot leave a Feishu meeting")
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--meeting-id", help="The internal meeting_id from /bots/join.")
    grp.add_argument("--meeting-no", help="Human-facing meeting number (will resolve via /bots/join).")
    args = parser.parse_args()
    return asyncio.run(_run(args.meeting_id, args.meeting_no))


if __name__ == "__main__":
    sys.exit(main())
