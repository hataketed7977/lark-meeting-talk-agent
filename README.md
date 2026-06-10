# Lark Meeting Talk Agent

A production-grade Python voice agent that joins a Feishu/Lark meeting as a bot, listens continuously, builds in-memory meeting context, and responds after wake-up in a continuous conversation.

It is built on Feishu/Lark VC realtime audio, Volcengine streaming ASR, an OpenAI-compatible LLM endpoint, and Volcengine streaming TTS.

## Features

- Joins meetings through Feishu/Lark `vc/v1/bots/join` and realtime audio APIs.
- Decodes/encodes Feishu realtime WebSocket frames with Frontier Frame and protobuf payloads.
- Runs always-on streaming ASR while staying silent in waiting mode.
- Uses wake-word gated dialog: silent until `Hey James` / `James`, then continuous conversation.
- Supports barge-in: user speech during TTS cancels LLM/TTS and clears Feishu playback buffers.
- Maintains in-memory meeting context for summaries, evaluations, decisions, risks, and action items.
- Streams LLM text into TTS sentence chunks for lower perceived latency.
- Runs as a single-process asyncio service with no broker or database requirement.

## Streaming Providers

- ASR uses a pluggable backend. The default `VOLC_ASR_BACKEND=sdk` path is built on the official `volcengine_audio` STT helpers and targets the recommended SAUC BigModel v3 async endpoint `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` with `VOLC_ASR_RESOURCE_ID=volc.bigasr.sauc.duration`. A legacy handcrafted WebSocket backend remains available as fallback.
- LLM replies use OpenAI-compatible streaming chat completions (`stream=True`) and are chunked into TTS as tokens arrive.
- TTS uses Volcengine HTTP V3 unidirectional streaming by default with `VOLC_TTS_RESOURCE_ID=seed-tts-2.0`.

## Architecture

```text
Feishu meeting audio
  -> Realtime WebSocket client
  -> Volcengine streaming ASR backend (SDK by default)
  -> WAITING / ENGAGED / SPEAKING state machine
  -> Meeting memory + OpenAI-compatible streaming LLM
  -> Volcengine streaming TTS (HTTP V3 / TTS 2.0)
  -> paced 24 kHz PCM audio back to Feishu
```

## Quick Start

### 1. Install

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

If you do not use editable install, the runtime dependencies are also listed in `requirements.txt`.

### 2. Generate protobuf stubs

Generated stubs are committed, so this is only needed after editing files in `proto/`.

```bash
cd proto
bash build.sh
cd ..
```

### 3. Configure

```bash
cp .env.example .env
```

Fill in `.env` with:

- Feishu/Lark app credentials and a user access token or refresh token.
- Volcengine ASR credentials. ASR defaults to `VOLC_ASR_BACKEND=sdk`,
  `VOLC_ASR_WS_URL=wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`,
  and `VOLC_ASR_RESOURCE_ID=volc.bigasr.sauc.duration`; Seed ASR 2.0 accounts
  can use `VOLC_ASR_RESOURCE_ID=volc.seedasr.sauc.duration`.
- OpenAI-compatible LLM endpoint, key, and model name via `LLM_*` variables.
- Volcengine TTS credentials. TTS defaults to the HTTP V3 TTS 2.0 character API
  with `VOLC_TTS_RESOURCE_ID=seed-tts-2.0` and the bilingual youth voice
  `zh_male_shaonianzixin_uranus_bigtts`; set `VOLC_TTS_API_VERSION=1.0` to use
  the legacy WebSocket TTS path.

VC bot join and realtime endpoint APIs require a Feishu/Lark `user_access_token`; app or tenant tokens are not enough for those calls. If `FEISHU_REFRESH_TOKEN`, `FEISHU_APP_ID`, and `FEISHU_APP_SECRET` are present, the agent can refresh the user token automatically.

### 4. Run

```bash
python -m lark_meeting_voice --meeting-no 123456789
```

Alternative modes:

```bash
python -m lark_meeting_voice --meeting-id 7642440384966134751
python -m lark_meeting_voice --ws-url 'wss://...'
```

## Wake And Conversation Behavior

- `WAITING`: ASR runs continuously and meeting memory is updated, but the bot does not speak.
- Wake words such as `hey james` or `james` move the bot to `ENGAGED`.
- `ENGAGED`: follow-up turns do not need another wake word.
- `SPEAKING`: any user speech can interrupt the bot and cancel the current reply.
- End-session intents such as `stop` or `结束` return the bot to `WAITING`.
- Meeting questions use compact in-memory context: rolling summary, structured facts, relevant transcript snippets, and recent utterances.

## Environment Variables

See `.env.example` for the complete list.

Important LLM variables are intentionally provider-neutral:

- `LLM_BASE_URL`: OpenAI-compatible API base URL.
- `LLM_API_KEY`: API key for the LLM provider.
- `LLM_MODEL`: model or endpoint identifier.
- `LLM_SYSTEM_PROMPT`: realtime voice assistant behavior prompt.
- `LLM_MAX_TOKENS`, `LLM_REQUEST_TIMEOUT_S`, `LLM_STREAM_IDLE_TIMEOUT_S`: latency and response controls.

## Development

Run checks locally:

```bash
python3 -m compileall lark_meeting_voice tests
python3 -m pytest
python3 -m lark_meeting_voice --help
```

## Security

- Never commit `.env`, access tokens, refresh tokens, API keys, logs, or QR images.
- `.gitignore` excludes local secrets and runtime artifacts by default.
- Use least-privilege Feishu/Lark app scopes and rotate credentials before publishing public demos.

## License

MIT License. See `LICENSE`.
