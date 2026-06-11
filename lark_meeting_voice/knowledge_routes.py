from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re


@dataclass(frozen=True)
class DocRoute:
    key: str
    title: str
    file_path: Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_KNOWLEDGE_DIR = _REPO_ROOT / "knowledge"

_DOC_ROUTES = {
    "lark-cli": DocRoute(
        key="lark-cli",
        title="Lark CLI",
        file_path=_KNOWLEDGE_DIR / "lark-cli.md",
    ),
}

_PRESENTATION_HELP_PATTERN = re.compile(
    r"(介绍|说明|讲讲|是什么|能做什么|分享|演示|present|introduc|explain|walk me through|help me)",
    re.IGNORECASE,
)
_CLI_SIGNAL_PATTERN = re.compile(
    r"(\bcli\b|\b[a-z]+cli\b|command line|命令行|工具链)",
    re.IGNORECASE,
)
_LARK_SIGNAL_PATTERN = re.compile(
    r"(\blark\b|\blarkcli\b|\blark-cli\b|\bluck\b|\bluxa?\b|飞书)",
    re.IGNORECASE,
)
_LARK_CLI_COMBINED_ALIAS_PATTERN = re.compile(
    r"(\blark[\s-]?cli\b|\bluck[\s-]?cli\b)",
    re.IGNORECASE,
)
_LARK_CLI_ASR_ALIAS_PATTERN = re.compile(
    r"\b(?:luxa?|lucks|lax|luck|lark)[\s.,]+(?:c[\s.,]+)?l[\s.,]*i\b\.?",
    re.IGNORECASE,
)
_SPELLED_CLI_PATTERN = re.compile(r"\bc[\s.,]+l[\s.,]*i\b\.?", re.IGNORECASE)


def _collapse_spelled_letters(text: str) -> str:
    def _join(match: re.Match[str]) -> str:
        return match.group(0).replace(" ", "")

    return re.sub(r"\b(?:[A-Za-z]\s+){2,}[A-Za-z]\b", _join, text)


def _normalize_brand_aliases(text: str) -> str:
    normalized = text
    normalized = _LARK_CLI_ASR_ALIAS_PATTERN.sub("lark cli", normalized)
    normalized = _SPELLED_CLI_PATTERN.sub("cli", normalized)
    normalized = re.sub(
        r"\bluck[\s-]?cli\b", "lark cli", normalized, flags=re.IGNORECASE
    )
    if _LARK_CLI_COMBINED_ALIAS_PATTERN.search(normalized):
        normalized = re.sub(r"\bluck\b", "lark", normalized, flags=re.IGNORECASE)
    return normalized


def _semantic_match_lark_cli(text: str) -> bool:
    has_lark_signal = bool(_LARK_SIGNAL_PATTERN.search(text))
    has_cli_signal = bool(_CLI_SIGNAL_PATTERN.search(text))
    wants_intro_or_help = bool(_PRESENTATION_HELP_PATTERN.search(text))
    return has_lark_signal and (has_cli_signal or wants_intro_or_help)


def match_doc_route(text: str) -> str | None:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return None
    normalized_variants = {
        cleaned,
        _collapse_spelled_letters(cleaned),
        _normalize_brand_aliases(cleaned),
        _normalize_brand_aliases(_collapse_spelled_letters(cleaned)),
    }
    if any(_semantic_match_lark_cli(candidate) for candidate in normalized_variants):
        return "lark-cli"
    return None


def canonicalize_doc_query(route_key: str, query: str) -> str:
    cleaned = " ".join((query or "").strip().split())
    if not cleaned:
        return cleaned
    if route_key != "lark-cli":
        return cleaned
    normalized = _normalize_brand_aliases(_collapse_spelled_letters(cleaned))
    normalized = re.sub(r"\blark[\s-]?cli\b", "Lark CLI", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\bluck[\s-]?cli\b", "Lark CLI", normalized, flags=re.IGNORECASE)
    normalized = re.sub(r"\blark\b", "Lark", normalized, flags=re.IGNORECASE)
    if _PRESENTATION_HELP_PATTERN.search(normalized):
        return (
            "Give a polished spoken introduction to Lark CLI for a live presentation. "
            "Start by saying it is the official open-source CLI for the Lark and Feishu "
            "platform. Then explain its main capabilities, why it matters for AI agents, "
            "and a simple quick start. Focus on Lark CLI itself, not on this meeting bot, "
            "unless the user explicitly asks about this project. Use natural English, give "
            "one cohesive answer of about four to six sentences, and write counts in English "
            "words instead of digits where natural. Original user request: "
            f"{normalized}"
        )
    if "lark cli" not in normalized.lower():
        normalized = f"About Lark CLI: {normalized}"
    return normalized


@lru_cache(maxsize=16)
def load_doc_markdown(route_key: str) -> str:
    route = _DOC_ROUTES.get(route_key)
    if route is None or not route.file_path.exists():
        return ""
    return route.file_path.read_text(encoding="utf-8").strip()


def build_doc_context(route_key: str, *, query: str = "", max_chars: int) -> str:
    route = _DOC_ROUTES.get(route_key)
    if route is None:
        return ""
    body = load_doc_markdown(route_key)
    if not body:
        return ""
    if _PRESENTATION_HELP_PATTERN.search(query or ""):
        guidance = (
            "The user is likely presenting this topic live and wants help with a "
            "cohesive introduction. Give a polished spoken overview, not a terse "
            "Q&A reply. Focus first on Lark CLI itself: what it is, what it does, "
            "why it matters for AI agents, and a simple quick start. Do not center "
            "the answer on this meeting project or say it mainly comes from meeting "
            "workflows unless the user explicitly asks how this project uses it. "
            "Prefer one connected answer of about four to six sentences that sounds "
            "ready to say aloud. If the user mentions a similar CLI name because of "
            "ASR noise, interpret it as this topic instead of saying you do not know. "
            "When mentioning counts or versions in English, prefer English words "
            "instead of bare digits where natural."
        )
    else:
        guidance = (
            "Use this note as the source of truth for this topic. Answer naturally "
            "for spoken delivery. Default to describing Lark CLI itself rather than "
            "this meeting project. If helpful, give a complete overview rather than "
            "a terse fragment. If the user mentions a phonetically similar CLI name, "
            "treat it as this topic instead of saying you do not know. Prefer English "
            "words for counts where natural."
        )
    text = f"Reference note: {route.title}\n" f"{guidance}\n\n" f"{body}"
    if max_chars > 0 and len(text) > max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    return text
