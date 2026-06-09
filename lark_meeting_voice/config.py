"""Centralized config. All credentials come from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from dotenv import load_dotenv

load_dotenv()


def _split_csv(s: str) -> List[str]:
    return [x.strip() for x in s.split(",") if x.strip()]


@dataclass
class FeishuConfig:
    host: str = os.getenv("LARK_OPENAPI_HOST", "https://open.feishu.cn")
    # VC / meeting Bot APIs (bots/join, realtime/endpoint) REQUIRE a
    # user_access_token. App / tenant tokens are rejected with 99991663.
    # Provide it via FEISHU_USER_ACCESS_TOKEN — this takes top priority.
    user_access_token: str = os.getenv("FEISHU_USER_ACCESS_TOKEN", "")
    refresh_token: str = os.getenv("FEISHU_REFRESH_TOKEN", "")
    user_access_token_expires_at: float = float(
        os.getenv("FEISHU_USER_ACCESS_TOKEN_EXPIRES_AT", "0") or "0"
    )
    # Fallback (only useful for OpenAPIs that DO accept tenant token).
    app_id: str = os.getenv("FEISHU_APP_ID", "")
    app_secret: str = os.getenv("FEISHU_APP_SECRET", "")
    pre_issued_token: str = os.getenv("FEISHU_TENANT_ACCESS_TOKEN", "")


@dataclass
class ASRConfig:
    appid: str = os.getenv("VOLC_ASR_APPID", "")
    token: str = os.getenv("VOLC_ASR_TOKEN", "")
    cluster: str = os.getenv("VOLC_ASR_CLUSTER", "volcengine_streaming_common")
    ws_url: str = os.getenv(
        "VOLC_ASR_WS_URL", "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel"
    )
    language: str = os.getenv("VOLC_ASR_LANGUAGE", "zh-CN")
    sample_rate: int = 16000
    connect_timeout_s: float = float(os.getenv("VOLC_ASR_CONNECT_TIMEOUT_S", "10"))
    stream_idle_timeout_s: float = float(
        os.getenv("VOLC_ASR_STREAM_IDLE_TIMEOUT_S", "20")
    )


@dataclass
class LLMConfig:
    base_url: str = os.getenv(
        "LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"
    )
    api_key: str = os.getenv("LLM_API_KEY", "")
    model: str = os.getenv("LLM_MODEL", "")
    system_prompt: str = os.getenv(
        "LLM_SYSTEM_PROMPT",
        (
            "You are James, a real-time meeting voice assistant. Reply in the user's "
            "language. Start with a direct one-sentence answer, then add at most "
            "3 concise bullets if useful. Ground answers in Meeting Memory; if the "
            "memory does not support an answer, say so briefly. For summaries or "
            "evaluations, use: overview, highlights, issues, suggestions. Keep most "
            "voice replies under 80 words unless the user asks for detail."
        ),
    )
    max_history_turns: int = int(os.getenv("LLM_MAX_HISTORY_TURNS", "8"))
    meeting_context_max_chars: int = int(os.getenv("MEETING_CONTEXT_MAX_CHARS", "6000"))
    max_tokens: int = int(os.getenv("LLM_MAX_TOKENS", "220"))
    request_timeout_s: float = float(os.getenv("LLM_REQUEST_TIMEOUT_S", "20"))
    stream_idle_timeout_s: float = float(
        os.getenv("LLM_STREAM_IDLE_TIMEOUT_S", "12")
    )
    tts_chunk_min_chars: int = int(os.getenv("LLM_TTS_CHUNK_MIN_CHARS", "6"))
    tts_chunk_max_chars: int = int(os.getenv("LLM_TTS_CHUNK_MAX_CHARS", "80"))


@dataclass
class TTSConfig:
    appid: str = os.getenv("VOLC_TTS_APPID", "")
    token: str = os.getenv("VOLC_TTS_TOKEN", "")
    cluster: str = os.getenv("VOLC_TTS_CLUSTER", "volcano_tts")
    ws_url: str = os.getenv(
        "VOLC_TTS_WS_URL", "wss://openspeech.bytedance.com/api/v1/tts/ws_binary"
    )
    voice_type: str = os.getenv(
        "VOLC_TTS_VOICE_TYPE", "zh_male_M392_conversation_wvae_bigtts"
    )
    sample_rate: int = 24000
    connect_timeout_s: float = float(os.getenv("VOLC_TTS_CONNECT_TIMEOUT_S", "10"))
    stream_idle_timeout_s: float = float(
        os.getenv("VOLC_TTS_STREAM_IDLE_TIMEOUT_S", "10")
    )


@dataclass
class AgentConfig:
    wake_words: List[str] = field(
        default_factory=lambda: _split_csv(
            os.getenv("WAKE_WORDS", "hey james,james,嘿james,嘿 james")
        )
    )
    stop_words: List[str] = field(
        default_factory=lambda: _split_csv(
            os.getenv(
                "STOP_WORDS",
                "stop,shut up,be quiet,enough,别说了,闭嘴,安静,住口,算了,停",
            )
        )
    )
    end_session_words: List[str] = field(
        default_factory=lambda: _split_csv(
            os.getenv(
                "END_SESSION_WORDS",
                "stop,thanks james,thank you james,结束,先这样,不用了,别说了",
            )
        )
    )
    engaged_idle_timeout_s: float = float(os.getenv("ENGAGED_IDLE_TIMEOUT_S", "60"))
    memory_recent_utterances: int = int(os.getenv("MEMORY_RECENT_UTTERANCES", "50"))
    memory_summary_items: int = int(os.getenv("MEMORY_SUMMARY_ITEMS", "8"))
    memory_context_recent_utterances: int = int(
        os.getenv("MEMORY_CONTEXT_RECENT_UTTERANCES", "16")
    )
    memory_rollup_utterances: int = int(os.getenv("MEMORY_ROLLUP_UTTERANCES", "10"))
    memory_rollup_max_chars: int = int(os.getenv("MEMORY_ROLLUP_MAX_CHARS", "1800"))
    memory_rollup_source_max_chars: int = int(
        os.getenv("MEMORY_ROLLUP_SOURCE_MAX_CHARS", "5000")
    )
    memory_retrieval_max_items: int = int(os.getenv("MEMORY_RETRIEVAL_MAX_ITEMS", "6"))
    reply_error_tts_text: str = os.getenv(
        "REPLY_ERROR_TTS_TEXT",
        "抱歉，我刚才没组织好回答，请再说一遍。",
    )
    reconnect_attempts: int = int(os.getenv("AGENT_RECONNECT_ATTEMPTS", "3"))
    reconnect_backoff_s: float = float(os.getenv("AGENT_RECONNECT_BACKOFF_S", "2.0"))
    log_level: str = os.getenv("LOG_LEVEL", "INFO")


@dataclass
class Config:
    feishu: FeishuConfig = field(default_factory=FeishuConfig)
    asr: ASRConfig = field(default_factory=ASRConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    tts: TTSConfig = field(default_factory=TTSConfig)
    agent: AgentConfig = field(default_factory=AgentConfig)

    def validate(
        self,
        *,
        require_feishu_access: bool = True,
        require_feishu_user_token: bool = False,
    ) -> None:
        missing: List[str] = []
        f = self.feishu
        if require_feishu_user_token and not (f.user_access_token or f.refresh_token):
            missing.append("FEISHU_USER_ACCESS_TOKEN or FEISHU_REFRESH_TOKEN")
        elif require_feishu_access and not (
            f.user_access_token
            or f.refresh_token
            or f.pre_issued_token
            or (f.app_id and f.app_secret)
        ):
            missing.append(
                "FEISHU_USER_ACCESS_TOKEN, FEISHU_REFRESH_TOKEN, FEISHU_APP_ID/FEISHU_APP_SECRET, or FEISHU_TENANT_ACCESS_TOKEN"
            )
        if not (self.asr.appid and self.asr.token):
            missing.append("VOLC_ASR_APPID / VOLC_ASR_TOKEN")
        if not (self.llm.api_key and self.llm.model):
            missing.append("LLM_API_KEY / LLM_MODEL")
        if not (self.tts.appid and self.tts.token):
            missing.append("VOLC_TTS_APPID / VOLC_TTS_TOKEN")
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )


CFG = Config()
