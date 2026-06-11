"""Volcengine ASR backend built on the official `volcengine_audio` package."""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Optional

import websockets
from volcengine_audio.stt import (
    ListenBidirectionPackage,
    VolcengineAsrFunctionsV3,
)

from lark_meeting_voice.asr.base import OnError, OnText
from lark_meeting_voice.asr.volc_asr import (
    _extract_response_headers,
    _is_v3_nostream_ws_url,
)
from lark_meeting_voice.config import CFG

log = logging.getLogger(__name__)


def _build_sdk_request_payload() -> dict[str, Any]:
    audio: dict[str, Any] = {
        "format": "pcm",
        "codec": "raw",
        "rate": CFG.asr.sample_rate,
        "bits": 16,
        "channel": 1,
    }
    if CFG.asr.language and _is_v3_nostream_ws_url(CFG.asr.ws_url):
        audio["language"] = CFG.asr.language
    return {
        "user": {"uid": "lark_meeting_voice"},
        "audio": audio,
        "request": {
            "model_name": "bigmodel",
            "enable_itn": True,
            "enable_punc": True,
            "show_utterances": True,
            "result_type": "single",
            "end_window_size": 800,
        },
    }


class SDKVolcASR:
    """Streaming ASR backend using official request/response helpers."""

    def __init__(
        self,
        *,
        on_partial: Optional[OnText] = None,
        on_final: Optional[OnText] = None,
        on_error: Optional[OnError] = None,
    ) -> None:
        self._on_partial = on_partial
        self._on_final = on_final
        self._on_error = on_error
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._recv_task: Optional[asyncio.Task] = None
        self._closed = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._last_partial = ""
        self._next_sequence = 1
        self._feed_count = 0
        self._recv_count = 0
        self._request_id = ""
        self._stop_lock = asyncio.Lock()
        self._stop_started = False
        self._final_frame_sent = False

    async def start(self) -> None:
        if "/api/v3/" not in CFG.asr.ws_url:
            raise RuntimeError(
                "SDKVolcASR requires a v3 ASR endpoint; use legacy backend for v2"
            )
        req_id = str(uuid.uuid4())
        self._request_id = req_id
        self._closed = asyncio.Event()
        self._next_sequence = 1
        self._stop_started = False
        self._final_frame_sent = False
        headers = {
            "X-Api-App-Key": CFG.asr.appid,
            "X-Api-Access-Key": CFG.asr.token,
            "X-Api-Resource-Id": CFG.asr.resource_id,
            "X-Api-Connect-Id": req_id,
            "X-Api-Request-Id": req_id,
            "X-Api-Sequence": "-1",
        }
        self._ws = await websockets.connect(
            CFG.asr.ws_url,
            additional_headers=headers,
            max_size=None,
            ping_interval=10,
            ping_timeout=10,
            open_timeout=CFG.asr.connect_timeout_s,
            close_timeout=5,
        )
        response_headers = _extract_response_headers(self._ws)
        frame = bytes(
            VolcengineAsrFunctionsV3.generate_asr_full_client_request(
                sequence=self._next_sequence,
                request_params=_build_sdk_request_payload(),
                compression=True,
            )
        )
        self._next_sequence += 1
        async with self._send_lock:
            await asyncio.wait_for(
                self._ws.send(frame), timeout=CFG.asr.connect_timeout_s
            )
        self._recv_task = asyncio.create_task(self._recv_loop(), name="asr-sdk-recv")
        log.info(
            "Volc ASR SDK backend started req_id=%s endpoint=%s resource_id=%s language=%s logid=%s connect_id=%s",
            req_id,
            CFG.asr.ws_url,
            CFG.asr.resource_id,
            CFG.asr.language,
            response_headers.get("X-Tt-Logid", ""),
            response_headers.get("X-Api-Connect-Id", ""),
        )

    async def feed_pcm(self, pcm16k: bytes) -> None:
        if not pcm16k:
            return
        self._feed_count += 1
        if self._feed_count == 1 or self._feed_count % 100 == 0:
            log.info("ASR feed chunk count=%d pcm_bytes=%d", self._feed_count, len(pcm16k))
        async with self._send_lock:
            if self._closed.is_set() or self._ws is None:
                return
            frame = bytes(
                VolcengineAsrFunctionsV3.generate_asr_audio_only_request(
                    sequence=self._next_sequence,
                    audio=pcm16k,
                    compress=True,
                    keep_sequence=True,
                )
            )
            self._next_sequence += 1
            try:
                await asyncio.wait_for(
                    self._ws.send(frame), timeout=CFG.asr.stream_idle_timeout_s
                )
            except asyncio.TimeoutError:
                self._closed.set()
                log.warning("ASR SDK send timed out req_id=%s", self._request_id)
                if self._on_error:
                    await self._on_error("asr_send_failed")
            except websockets.ConnectionClosed as exc:
                self._closed.set()
                log.warning(
                    "ASR SDK send failed on closed socket req_id=%s code=%s reason=%s",
                    self._request_id,
                    getattr(exc, "code", None),
                    getattr(exc, "reason", ""),
                )
                if self._on_error:
                    await self._on_error("asr_send_failed")

    async def stop(self) -> None:
        async with self._stop_lock:
            if self._stop_started:
                log.info("ASR SDK stop already in progress req_id=%s", self._request_id)
                return
            self._stop_started = True
            self._closed.set()
            ws = self._ws
            self._ws = None
        if ws is not None:
            try:
                async with self._send_lock:
                    if not self._final_frame_sent:
                        frame = bytes(
                            VolcengineAsrFunctionsV3.generate_asr_audio_only_request(
                                sequence=self._next_sequence,
                                audio=b"",
                                compress=False,
                                keep_sequence=False,
                            )
                        )
                        await ws.send(frame)
                        self._final_frame_sent = True
            except Exception:  # noqa: BLE001
                log.exception("ASR SDK final frame send failed req_id=%s", self._request_id)
            try:
                await ws.close()
                log.info(
                    "ASR SDK websocket closed req_id=%s close_code=%s close_reason=%s",
                    self._request_id,
                    getattr(ws, "close_code", None),
                    getattr(ws, "close_reason", ""),
                )
            except Exception:  # noqa: BLE001
                log.exception("ASR SDK websocket close failed req_id=%s", self._request_id)
        if self._recv_task:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except (asyncio.CancelledError, Exception):
                pass

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        ws = self._ws
        try:
            while not self._closed.is_set():
                try:
                    msg = await asyncio.wait_for(
                        ws.recv(), timeout=CFG.asr.stream_idle_timeout_s
                    )
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed as exc:
                    log.warning(
                        "ASR SDK websocket recv closed req_id=%s code=%s reason=%s",
                        self._request_id,
                        getattr(exc, "code", None),
                        getattr(exc, "reason", ""),
                    )
                    break
                if not isinstance(msg, (bytes, bytearray)):
                    continue
                parsed = VolcengineAsrFunctionsV3.parse_response(bytes(msg))
                self._recv_count += 1
                if self._recv_count == 1:
                    log.info("ASR SDK first frame keys=%s", sorted(parsed.keys()))
                if "code" in parsed:
                    code = parsed.get("code")
                    log.error("ASR SDK server error code=%s payload=%s", code, parsed)
                    if self._on_error:
                        await self._on_error(f"asr_error:{code}")
                    continue
                package = self._coerce_package(parsed)
                if package is None:
                    continue
                await self._dispatch_package(package)
        except Exception as exc:  # noqa: BLE001
            log.exception("ASR SDK recv loop crashed: %s", exc)
        finally:
            self._closed.set()

    def _coerce_package(
        self, parsed: dict[str, Any]
    ) -> ListenBidirectionPackage | None:
        message = parsed.get("message")
        if not isinstance(message, dict):
            return None
        result = message.get("result")
        if not isinstance(result, dict):
            return None
        if not (result.get("text") or result.get("utterances")):
            return None
        try:
            return ListenBidirectionPackage.model_validate(
                {
                    "is_last_package": parsed.get("is_last_package", False),
                    "sequence": parsed.get("sequence", 0),
                    "message": message,
                    "size": parsed.get("size", 0),
                }
            )
        except ValidationError:
            log.info(
                "ASR SDK unsupported payload keys=%s message_keys=%s",
                sorted(result.keys()),
                sorted(message.keys()),
            )
            return None

    async def _dispatch_package(self, package: ListenBidirectionPackage) -> None:
        result = package.message.result
        text = result.text.strip()
        utterances = result.utterances or []
        final_text = "".join(u.text for u in utterances if u.definite).strip()
        explicit_final = bool(package.is_last_package)
        if not final_text and explicit_final and text:
            final_text = text

        if final_text and final_text != self._last_partial:
            self._last_partial = ""
            if self._on_final:
                await self._on_final(final_text)
            return

        if text and text != self._last_partial:
            self._last_partial = text
            if self._on_partial:
                await self._on_partial(text)
