"""Token provider for Feishu Open APIs.

Meeting Bot OpenAPIs (bots/join, realtime/endpoint) REQUIRE a user_access_token —
app/tenant tokens are rejected with 99991663. So we always prefer
FEISHU_USER_ACCESS_TOKEN / FEISHU_REFRESH_TOKEN when present.

Priority:
  1. FEISHU_USER_ACCESS_TOKEN + FEISHU_REFRESH_TOKEN (refreshable user token)
  2. FEISHU_USER_ACCESS_TOKEN                        (static user token)
  3. FEISHU_TENANT_ACCESS_TOKEN                      (pre-issued, no refresh)
  4. FEISHU_APP_ID/SECRET                            (auto-refresh tenant_access_token)
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
import time
from typing import Optional

import aiohttp
from dotenv import find_dotenv

from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)


def _env_path() -> Path:
    dotenv_path = find_dotenv(usecwd=True)
    return Path(dotenv_path) if dotenv_path else Path.cwd() / ".env"


def _set_env_values(values: dict[str, str]) -> None:
    path = _env_path()
    existing = path.read_text().splitlines() if path.exists() else []
    seen: set[str] = set()
    lines: list[str] = []

    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            lines.append(line)
            continue

        key = line.split("=", 1)[0].strip()
        if key in values:
            lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            lines.append(line)

    for key, value in values.items():
        if key not in seen:
            lines.append(f"{key}={value}")

    path.write_text("\n".join(lines) + "\n")
    for key, value in values.items():
        os.environ[key] = value


class TokenProvider:
    def __init__(self) -> None:
        self._user_refresh_token: Optional[str] = CFG.feishu.refresh_token or None
        self._pre_issued_token: Optional[str] = CFG.feishu.pre_issued_token or None
        self._token: Optional[str] = (
            CFG.feishu.user_access_token or self._pre_issued_token or None
        )
        self._expires_at: float = self._initial_expires_at()
        self._lock = asyncio.Lock()
        if CFG.feishu.user_access_token and self._user_refresh_token:
            log.info("Using refreshable Feishu user access token")
        elif self._user_refresh_token:
            log.info("Using FEISHU_REFRESH_TOKEN to obtain user access token")
        elif CFG.feishu.user_access_token:
            log.info("Using static FEISHU_USER_ACCESS_TOKEN (user identity)")
        elif CFG.feishu.pre_issued_token:
            log.info("Using FEISHU_TENANT_ACCESS_TOKEN (tenant identity, pre-issued)")
        else:
            log.info("Using app_id/secret to auto-refresh tenant_access_token")

    def _initial_expires_at(self) -> float:
        if self._pre_issued_token:
            return float("inf")
        if self._user_refresh_token:
            return CFG.feishu.user_access_token_expires_at
        if CFG.feishu.user_access_token:
            return float("inf")
        return 0.0

    @property
    def can_refresh_user_token(self) -> bool:
        return bool(
            self._user_refresh_token and CFG.feishu.app_id and CFG.feishu.app_secret
        )

    async def get(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._expires_at - 60:
                return self._token
            if self.can_refresh_user_token:
                await self._refresh_user_access_token()
            elif self._pre_issued_token or CFG.feishu.user_access_token:
                assert self._token
                return self._token
            else:
                await self._refresh_tenant_access_token()
            assert self._token
            return self._token

    async def refresh_user_access_token(self) -> str:
        async with self._lock:
            await self._refresh_user_access_token()
            assert self._token
            return self._token

    async def _refresh_user_access_token(self) -> None:
        if not self.can_refresh_user_token:
            raise RuntimeError(
                "FEISHU_REFRESH_TOKEN refresh requires FEISHU_APP_ID and FEISHU_APP_SECRET"
            )

        url = f"{CFG.feishu.host}/open-apis/authen/v2/oauth/token"
        payload = {
            "grant_type": "refresh_token",
            "client_id": CFG.feishu.app_id,
            "client_secret": CFG.feishu.app_secret,
            "refresh_token": self._user_refresh_token,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json; charset=utf-8"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as r:
                data = await r.json()

        if data.get("code") not in (None, 0):
            raise RuntimeError(f"user_access_token refresh failed: {data}")
        access_token = data.get("access_token") or data.get("user_access_token")
        refresh_token = data.get("refresh_token")
        if not access_token:
            raise RuntimeError(
                f"user_access_token refresh returned no access token: {data}"
            )

        expires_in = int(data.get("expires_in") or data.get("expire") or 7200)
        self._token = access_token
        self._expires_at = time.time() + expires_in
        if refresh_token:
            self._user_refresh_token = refresh_token

        values = {
            "FEISHU_USER_ACCESS_TOKEN": access_token,
            "FEISHU_USER_ACCESS_TOKEN_EXPIRES_AT": str(int(self._expires_at)),
        }
        if refresh_token:
            values["FEISHU_REFRESH_TOKEN"] = refresh_token
        _set_env_values(values)

        CFG.feishu.user_access_token = access_token
        CFG.feishu.user_access_token_expires_at = self._expires_at
        if refresh_token:
            CFG.feishu.refresh_token = refresh_token

        log.info("Refreshed Feishu user_access_token, expires in %ss", expires_in)

    async def _refresh_tenant_access_token(self) -> None:
        url = f"{CFG.feishu.host}/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": CFG.feishu.app_id,
            "app_secret": CFG.feishu.app_secret,
        }
        async with aiohttp.ClientSession() as s:
            async with s.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=10)
            ) as r:
                data = await r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"tenant_access_token failed: {data}")
        self._token = data["tenant_access_token"]
        self._expires_at = time.time() + int(data.get("expire", 7200))
        log.info("Refreshed tenant_access_token, expires in %ss", data.get("expire"))


TOKEN_PROVIDER = TokenProvider()
