import json
from pathlib import Path

from lark_meeting_voice.lark.artifact_fetcher import (
    _collect_text_files,
    _extract_docx_text,
    _extract_missing_scopes,
)


def test_extract_docx_text_prefers_data_content():
    payload = {
        "code": 0,
        "data": {
            "content": "Meeting note body",
            "raw_content": "fallback",
        },
    }

    assert _extract_docx_text(payload) == "Meeting note body"


def test_collect_text_files_reads_markdown_text_and_json(tmp_path: Path):
    out = tmp_path / "artifacts"
    out.mkdir()
    (out / "note.md").write_text("# Title\nSummary", encoding="utf-8")
    (out / "verbatim.txt").write_text("hello\nworld", encoding="utf-8")
    (out / "meta.json").write_text(
        json.dumps({"minute_token": "abc123", "title": "Weekly Sync"}),
        encoding="utf-8",
    )
    (out / "binary.bin").write_bytes(b"\x00\x01")

    text = _collect_text_files(out)

    assert "# note.md" in text
    assert "# Title\nSummary" in text
    assert "# verbatim.txt" in text
    assert "hello\nworld" in text
    assert "# meta.json" in text
    assert '"minute_token": "abc123"' in text
    assert "binary.bin" not in text


def test_extract_missing_scopes_reads_json_objects_from_mixed_output():
    text = """
    [vc +notes] querying minute_token=abc123
    {"ok": false, "error": {"missing_scopes": ["minutes:minutes:readonly", "minutes:minutes.artifacts:read"]}}
    trailing log
    """

    assert _extract_missing_scopes(text) == [
        "minutes:minutes:readonly",
        "minutes:minutes.artifacts:read",
    ]
