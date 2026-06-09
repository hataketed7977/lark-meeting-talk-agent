from lark_meeting_voice.audio.framer import FRAME_BYTES, split_pcm


def test_split_aligned():
    pcm = b"\x01\x02" * (FRAME_BYTES * 2 // 2)
    chunks = split_pcm(pcm)
    assert all(len(c) == FRAME_BYTES for c in chunks)
    assert b"".join(chunks) == pcm


def test_split_unaligned_drops_dangling_byte():
    pcm = b"\xff" * (FRAME_BYTES + 3)  # 3 trailing bytes -> tail should be 2
    chunks = split_pcm(pcm)
    assert len(chunks[0]) == FRAME_BYTES
    assert len(chunks[1]) == 2
    assert sum(len(c) for c in chunks) % 2 == 0


def test_split_short():
    pcm = b"\x00\x01\x02\x03"
    chunks = split_pcm(pcm)
    assert chunks == [pcm]
