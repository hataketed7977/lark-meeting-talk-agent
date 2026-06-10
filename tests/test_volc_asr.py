import asyncio

from lark_meeting_voice.asr.volc_asr import VolcASR


def test_handle_result_treats_explicit_final_without_utterances_as_final():
    seen: list[str] = []

    async def on_final(text: str) -> None:
        seen.append(text)

    async def run() -> None:
        asr = VolcASR(on_final=on_final)
        await asr._handle_result(  # noqa: SLF001
            {
                "sequence": -1,
                "payload": {
                    "result": [
                        {
                            "text": "hey james are you there",
                            "utterances": [],
                        }
                    ]
                },
            }
        )

    asyncio.run(run())
    assert seen == ["hey james are you there"]
