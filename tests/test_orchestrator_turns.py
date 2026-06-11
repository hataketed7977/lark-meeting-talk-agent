from lark_meeting_voice.agent.orchestrator import (
    _has_incomplete_english_tail,
)


def test_incomplete_english_tail_blocks_soft_final():
    assert _has_incomplete_english_tail("One more question. So how do you feel about.")
    assert _has_incomplete_english_tail("Yeah, my question is.")
    assert _has_incomplete_english_tail("Actually, I want to")


def test_complete_english_question_does_not_block_soft_final():
    assert not _has_incomplete_english_tail("How do you feel about this meeting?")
    assert not _has_incomplete_english_tail("Can you introduce Lark CLI?")
