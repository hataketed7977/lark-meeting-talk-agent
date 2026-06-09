# Lark Meeting Talk Agent

A production-grade Python voice agent that joins a Feishu (Lark) meeting as a bot,
listens continuously, builds in-memory meeting context, and responds after wake-up
in a continuous conversation. Built on Volcengine ASR + Doubao LLM + Volcengine TTS.

## Features

- Bot auto-joins meeting via `vc/v1/bots/join`
- Realtime WebSocket: Frontier Frame + protobuf double-encoding done correctly
- Always-on Volcengine streaming ASR (24 kHz downstream → 16 kHz)
- Wake-word gated dialog: silent until "Hey James" / "James" detected, then
  continuous conversation until an end-session intent
- Hard barge-in: any user speech during TTS playback → cancel LLM, cancel TTS,
  and send `audio.upstream.clear` to wipe Feishu's playback buffer
- Intent classification: STOP intents only interrupt, never trigger a reply
- Volcengine streaming TTS → 24 kHz s16le mono → pacing-aware upstream frames
- Single-process asyncio, no Docker, no broker

## Quick Start

```bash
# 1. Generate protobuf stubs (once)
cd proto && bash build.sh && cd ..

# 2. Install deps
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# edit .env, fill in all credentials

# 4. Run (meeting_no = the numeric meeting number a user sees in Feishu)
python -m lark_meeting_voice --meeting-no 123456789
```

## Wake / Conversation behavior

- Always listens. ASR runs continuously.
- In `WAITING`, the bot records meeting memory but does not speak.
- A wake word such as "hey james" or "james" moves the bot to `ENGAGED`.
- In `ENGAGED`, follow-up turns do not need another wake word.
- During TTS playback, any user speech immediately interrupts. If that
-  utterance is a STOP/end-session intent (e.g. "stop", "shut up", "别说了"),
  the bot stops speaking and can return to waiting depending on the configured
  intent list.
- Meeting questions use compact in-memory context: rolling summary, structured
  facts, relevant transcript snippets, and recent utterances.

## Environment Variables

See `.env.example`.
