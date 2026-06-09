from lark_meeting_voice.intent.stop_classifier import StopClassifier


STOP = ["stop", "shut up", "be quiet", "enough", "别说了", "闭嘴", "安静", "住口", "算了", "停"]


def test_match():
    c = StopClassifier(STOP)
    assert c.is_stop("Stop, please.")
    assert c.is_stop("be quiet")
    assert c.is_stop("好了好了 闭嘴")
    assert c.is_stop("算了")


def test_no_match():
    c = StopClassifier(STOP)
    assert not c.is_stop("hey james what is on my agenda")
    assert not c.is_stop("")
    assert not c.is_stop("keep going")
