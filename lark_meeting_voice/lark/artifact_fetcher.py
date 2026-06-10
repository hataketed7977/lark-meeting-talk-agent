from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from tempfile import mkdtemp

import aiohttp

from lark_meeting_voice.config import CFG
from lark_meeting_voice.lark.auth import TOKEN_PROVIDER
from lark_meeting_voice.memory.meeting_memory import MeetingArtifact

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ArtifactContent:
    kind: str
    token: str
    content: str


class ArtifactFetchError(RuntimeError):
    pass


async def fetch_artifact_content(artifact: MeetingArtifact) -> ArtifactContent:
    if artifact.kind in {"note", "verbatim"}:
        text = await _fetch_docx_raw_content(artifact.token)
        return ArtifactContent(kind=artifact.kind, token=artifact.token, content=text)
    if artifact.kind == "minute":
        text = await _fetch_minute_notes(artifact.token)
        return ArtifactContent(kind=artifact.kind, token=artifact.token, content=text)
    raise ArtifactFetchError(f"unsupported artifact kind: {artifact.kind}")


async def _fetch_docx_raw_content(document_id: str) -> str:
    token = await TOKEN_PROVIDER.get()
    url = f"{CFG.feishu.host}/open-apis/docx/v1/documents/{document_id}/raw_content"
    headers = {"Authorization": f"Bearer {token}"}
    timeout = aiohttp.ClientTimeout(total=15)

    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=timeout) as response:
            payload = await response.json()

    if payload.get("code") not in (None, 0):
        raise ArtifactFetchError(f"docx raw content failed: {payload}")
    text = _extract_docx_text(payload)
    if not text:
        raise ArtifactFetchError("docx raw content returned empty text")
    return text


async def _fetch_minute_notes(minute_token: str) -> str:
    temp_root = Path(mkdtemp(prefix="minute-artifact-", dir=Path.cwd()))
    output_dir = temp_root / "out"
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "lark-cli",
        "vc",
        "+notes",
        "--minute-tokens",
        minute_token,
        "--as",
        "user",
        "--json",
        "--overwrite",
        "--output-dir",
        str(output_dir.relative_to(Path.cwd())),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        combined = "\n".join(
            part.strip()
            for part in (
                stdout.decode("utf-8", errors="replace"),
                stderr.decode("utf-8", errors="replace"),
            )
            if part.strip()
        )
        if proc.returncode != 0:
            scopes = _extract_missing_scopes(combined)
            if scopes:
                raise ArtifactFetchError(
                    "minute artifact scopes missing: " + ", ".join(scopes)
                )
            raise ArtifactFetchError(f"vc +notes failed: {combined or proc.returncode}")
        text = _collect_text_files(output_dir)
        if not text:
            raise ArtifactFetchError("vc +notes produced no readable artifact text")
        return text
    finally:
        shutil.rmtree(temp_root, ignore_errors=True)


def _extract_docx_text(payload: dict) -> str:
    data = payload.get("data") or {}
    candidates = [
        data.get("content"),
        data.get("raw_content"),
        data.get("text"),
        payload.get("content"),
        payload.get("raw_content"),
        payload.get("text"),
    ]
    for item in candidates:
        if isinstance(item, str) and item.strip():
            return item.strip()
    return ""


def _collect_text_files(output_dir: Path) -> str:
    chunks: list[str] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".txt", ".json"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        cleaned = text.strip()
        if not cleaned:
            continue
        if path.suffix.lower() == ".json":
            try:
                parsed = json.loads(cleaned)
            except json.JSONDecodeError:
                pass
            else:
                cleaned = json.dumps(parsed, ensure_ascii=False, indent=2)
        chunks.append(f"# {path.name}\n{cleaned}")
    return "\n\n".join(chunks)


def _extract_missing_scopes(text: str) -> list[str]:
    scopes: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped.startswith('"missing_scopes"'):
            continue
        # Non-line-oriented JSON is handled below.
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        try:
            obj, end = decoder.raw_decode(text, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        idx = end
        error = (obj.get("error") or {}) if isinstance(obj, dict) else {}
        missing = error.get("missing_scopes")
        if isinstance(missing, list):
            scopes.extend(str(item) for item in missing if str(item).strip())
    return scopes
