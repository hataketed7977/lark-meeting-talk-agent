import base64

from lark_meeting_voice.tts.volc_tts import (
    _decode_http_v3_audio,
    _pop_stream_json_objects,
)


def test_pop_stream_json_objects_parses_sse_and_concatenated_json():
    audio = base64.b64encode(b"pcm").decode("ascii")
    buf = f'data: {{"code":0,"sequence":1,"data":"{audio}"}}\n{{"sequence":-1}}\n'

    items, rest = _pop_stream_json_objects(buf)

    assert rest == ""
    assert items == [
        {"code": 0, "sequence": 1, "data": audio},
        {"sequence": -1},
    ]


def test_decode_http_v3_audio_returns_pcm_and_done_signal():
    audio = base64.b64encode(b"pcm").decode("ascii")

    chunk, done = _decode_http_v3_audio({"code": 0, "sequence": "1", "data": audio})
    final_chunk, final_done = _decode_http_v3_audio({"sequence": -1})

    assert chunk == b"pcm"
    assert done is False
    assert final_chunk == b""
    assert final_done is True


def test_decode_http_v3_audio_treats_success_without_data_as_clean_end():
    chunk, done = _decode_http_v3_audio(
        {"code": 20000000, "message": "OK", "data": None}
    )

    assert chunk == b""
    assert done is True
