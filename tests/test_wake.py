from lark_meeting_voice.wake.detector import WakeDetector


WAKE = ["hey james", "james", "嘿james", "嘿 james"]


def test_simple():
    d = WakeDetector(WAKE)
    assert d.is_wake("Hey James, what time is it?")
    assert d.is_wake("hey james")
    assert d.is_wake("James!")
    assert d.is_wake("嘿 James,帮我记一下")
    assert d.is_wake("嘿james 现在几点")


def test_negative():
    d = WakeDetector(WAKE)
    assert not d.is_wake("Hello team")
    assert not d.is_wake("Jim please help")
    assert not d.is_wake("")


def test_strip():
    d = WakeDetector(WAKE)
    assert d.strip_wake("hey james, what time is it") == "what time is it"
    assert d.strip_wake("James help me") == "help me"
