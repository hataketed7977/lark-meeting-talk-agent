from lark_meeting_voice.knowledge_routes import (
    canonicalize_doc_query,
    match_doc_route,
)


def test_lark_cli_route_matches_asr_lux_l_i_alias():
    text = "Actually, I want to talk about Lux L. I. Can you help me introduce it?"

    assert match_doc_route(text) == "lark-cli"


def test_lark_cli_route_matches_asr_luxa_i_alias():
    text = "Actually, I'm talking about LUXA. I. So could you please introduce it?"

    assert match_doc_route(text) == "lark-cli"


def test_lark_cli_route_matches_spelled_cli_with_punctuation():
    text = "No, I mean Lark, C L, I."

    assert match_doc_route(text) == "lark-cli"


def test_lark_cli_query_canonicalizes_asr_lux_l_i_alias():
    query = canonicalize_doc_query(
        "lark-cli",
        "Actually, I want to talk about Lux L. I. Can you help me introduce it?",
    )

    assert "Lark CLI" in query
    assert "Lux L. I." not in query
