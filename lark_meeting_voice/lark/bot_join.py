"""Bot join / leave Feishu meetings."""

from __future__ import annotations

import logging
from typing import Tuple

import aiohttp

from lark_meeting_voice.config import CFG
from lark_meeting_voice.lark.auth import TOKEN_PROVIDER

log = logging.getLogger(__name__)


_AUTH_ERROR_CODES = {99991663, 99991677}


class LarkAPIError(RuntimeError):
    def __init__(self, operation: str, payload: dict) -> None:
        self.operation = operation
        self.payload = payload
        self.code = payload.get("code")
        super().__init__(f"{operation} failed: {payload}")

    @property
    def retryable(self) -> bool:
        # Invalid/expired tokens are caller action items; retrying just burns time.
        return self.code not in _AUTH_ERROR_CODES


async def _request_with_user_token_refresh(method: str, url: str, **kwargs) -> dict:
    token = await TOKEN_PROVIDER.get()
    headers = dict(kwargs.pop("headers", {}))
    timeout = kwargs.pop("timeout")
    retry_timeout = kwargs.pop("retry_timeout", timeout)
    headers["Authorization"] = f"Bearer {token}"

    async with aiohttp.ClientSession() as s:
        async with s.request(
            method, url, headers=headers, timeout=timeout, **kwargs
        ) as r:
            data = await r.json()

    if data.get("code") in _AUTH_ERROR_CODES and TOKEN_PROVIDER.can_refresh_user_token:
        log.info("Feishu user token rejected; refreshing and retrying once")
        token = await TOKEN_PROVIDER.refresh_user_access_token()
        headers["Authorization"] = f"Bearer {token}"
        async with aiohttp.ClientSession() as s:
            async with s.request(
                method,
                url,
                headers=headers,
                timeout=retry_timeout,
                **kwargs,
            ) as r:
                data = await r.json()

    return data


async def bot_join_meeting(meeting_no: str) -> Tuple[str, dict]:
    """POST /open-apis/vc/v1/bots/join.

    Returns (meeting_id, raw_meeting_obj).
    """
    url = f"{CFG.feishu.host}/open-apis/vc/v1/bots/join"
    body = {
        "join_type": 1,
        "join_identify": {"meeting_no": meeting_no},
    }
    headers = {
        "Content-Type": "application/json",
    }
    data = await _request_with_user_token_refresh(
        "POST",
        url,
        json=body,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=15),
        retry_timeout=aiohttp.ClientTimeout(total=15),
    )
    if data.get("code") != 0:
        raise LarkAPIError("bots/join", data)
    meeting = data["data"]["meeting"]
    log.info("Bot joined meeting %s (id=%s)", meeting_no, meeting["id"])
    return meeting["id"], meeting


async def get_realtime_endpoint(meeting_id: str) -> str:
    url = (
        f"{CFG.feishu.host}/open-apis/vc/v1/realtime/endpoint"
        f"?meeting_id={meeting_id}"
    )
    data = await _request_with_user_token_refresh(
        "GET",
        url,
        timeout=aiohttp.ClientTimeout(total=10),
        retry_timeout=aiohttp.ClientTimeout(total=10),
    )
    if data.get("code") != 0:
        raise LarkAPIError("realtime/endpoint", data)
    return data["data"]["websocket_url"]


async def bot_leave_meeting(meeting_id: str) -> dict:
    """POST /open-apis/vc/v1/bots/leave.

    Tells the Feishu VC backend the bot is leaving the meeting. Safe to call
    multiple times — already-left returns a non-fatal code that we just log.
    """
    url = f"{CFG.feishu.host}/open-apis/vc/v1/bots/leave"
    body = {"meeting_id": meeting_id}
    headers = {
        "Content-Type": "application/json",
    }
    data = await _request_with_user_token_refresh(
        "POST",
        url,
        json=body,
        headers=headers,
        timeout=aiohttp.ClientTimeout(total=10),
        retry_timeout=aiohttp.ClientTimeout(total=10),
    )
    if data.get("code") != 0:
        log.warning("bots/leave returned non-zero: %s", data)
    else:
        log.info("Bot left meeting id=%s", meeting_id)
    return data
