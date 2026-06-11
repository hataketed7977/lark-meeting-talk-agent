# AGENTS.md

This file is the project-specific operating guide for TRAE and similar coding agents working in this repository.

## Project Intent

- Project: `lark-meeting-talk-agent`
- Python package: `lark_meeting_voice`
- Purpose: a Feishu/Lark meeting voice bot that joins a meeting, listens continuously, keeps meeting memory, and responds after wake-up.
- Primary demo mode: English-first meeting assistant with stable live behavior preferred over experimental architecture.

## Working Directory

- Repository root: `/Users/bytedance/workspace/open/lark-meeting-talk-agent`
- Default shell commands should run from the repository root.
- Prefer the local virtualenv at `.venv`.

## Runtime Priority

When tradeoffs appear, optimize in this order:

1. Stable live demo behavior
2. Fast reply latency
3. English ASR accuracy
4. Natural bilingual TTS
5. Architecture cleanliness

Do not switch the runtime to a newer ASR path just because README defaults mention it. This project has already been tuned toward a more stable local runtime.

## Current Stable Local Runtime

Use the local `.env` as the runtime source of truth. At the time this file was written, the preferred local settings are:

```env
VOLC_ASR_BACKEND=legacy
VOLC_ASR_WS_URL=wss://openspeech.bytedance.com/api/v2/asr
VOLC_ASR_LANGUAGE=en-US
LLM_MODEL=doubao-seed-2-0-mini-260428
VOLC_TTS_VOICE_TYPE=zh_male_m191_uranus_bigtts
ENGAGED_IDLE_TIMEOUT_S=0
LLM_TTS_CHUNK_MIN_CHARS=12
LLM_TTS_CHUNK_MAX_CHARS=100
```

Notes:

- `ENGAGED_IDLE_TIMEOUT_S` must stay single-defined. Duplicate keys in `.env` caused a real bug before, where a later `60` overrode the intended `0`.
- README may describe older defaults such as SDK ASR or another TTS voice. For live operation, prefer the actual local `.env`.
- Keep `.env` local. Do not commit secrets or local demo-only tuning unless explicitly asked.

## Standard Operations

### Start And Join A Meeting

When the user asks to join a meeting and only provides the meeting number, use:

```bash
.venv/bin/python -u -m lark_meeting_voice --meeting-no 616633662
```

General form:

```bash
.venv/bin/python -u -m lark_meeting_voice --meeting-no <meeting_no>
```

Alternative forms supported by the code:

```bash
.venv/bin/python -u -m lark_meeting_voice --meeting-id <meeting_id>
.venv/bin/python -u -m lark_meeting_voice --ws-url '<realtime_ws_url>'
```

### Leave A Meeting

If the user asks the bot to leave a meeting, prefer the dedicated leave entrypoint:

```bash
.venv/bin/python -u -m lark_meeting_voice.leave --meeting-no <meeting_no>
```

Or:

```bash
.venv/bin/python -u -m lark_meeting_voice.leave --meeting-id <meeting_id>
```

### Stop The Current Bot Process

If a meeting bot is already running in a terminal, stop that command before starting a replacement process. Avoid multiple competing bot processes for the same meeting.

Mandatory restart pattern:

1. Check whether a prior bot process is still running
2. Stop the prior bot process cleanly
3. Explicitly leave the meeting with `.venv/bin/python -u -m lark_meeting_voice.leave --meeting-no <meeting_no>` or the `meeting_id` form before rejoining
4. Confirm there is no remaining `python -u -m lark_meeting_voice` process for the same meeting number
5. Start exactly one fresh bot process
6. Verify readiness logs before telling the user the bot is ready

Do not skip the leave step when the user asks to "restart", "retry", or "rejoin". In this project, restart means `stop -> leave -> verify single-process-zero -> start one new join`.

Recommended verification command before and after restart:

```bash
pgrep -fl "python -u -m lark_meeting_voice"
```

If more than one bot process is visible for the same meeting, treat that as an operational error and clean it up before proceeding.

## Readiness Checks

After starting the bot, check logs for these signals:

- `Realtime session ready; entering orchestrator loop`
- `Meeting event consumers started`
- `ASR backend started`
- `Volc ASR stream started ... language=en-US`

If these appear, the bot is usually ready for live interaction.

## Fast Triage Guide

If the user says "no response" or "it stopped responding", check which layer failed:

- No `ASR final`: likely audio capture, ASR connectivity, or speaking conditions
- Has `ASR final` but no reply start: likely state machine or wake/engaged logic
- Has reply start but slow: likely LLM first token latency
- Has LLM output but no audio: likely TTS or realtime playback path

Useful log patterns:

- `ASR final:`
- `Reply START`
- `LLM first token latency=`
- `TTS audio started`
- `Reply DONE`
- `Engaged session idle timeout reached -> WAITING`

Important known issue:

- If the bot unexpectedly returns to `WAITING`, inspect `.env` for duplicate `ENGAGED_IDLE_TIMEOUT_S` entries before assuming a code regression.

## Meeting Memory And Event Model

The intended product behavior is:

- ASR is mainly for wake word and turn-by-turn conversation
- Meeting summary and long-form memory should come from current-meeting-scoped event consumers
- Event consumption must be scoped to the current meeting, not all meetings

Do not regress this product direction.

## TTS Voice Guidance

Current preferred demo voice:

- `zh_male_m191_uranus_bigtts`

Known tradeoff:

- This voice sounds best overall for the current demo, but can feel slightly "oily" or over-performed

Previously tried voices:

- `zh_female_yingyujiaoxue_uranus_bigtts`
- `zh_male_dayi_uranus_bigtts`

Unless the user asks to audition voices again, keep `zh_male_m191_uranus_bigtts`.

## Repo Hygiene

Before making changes, check:

```bash
git status --short
git remote -v
```

Rules:

- Do not revert user changes unless explicitly asked
- Treat `.env` as local-only runtime config
- Treat `.gitignore` changes as user-owned unless confirmed otherwise
- If unexpected modifications appear in unrelated tracked files, pause and ask

## Validation

When code changes are made, useful local checks are:

```bash
python3 -m compileall lark_meeting_voice tests
python3 -m pytest
python3 -m lark_meeting_voice --help
```

For local-only `.env` changes, validate by restarting the bot and checking logs rather than running full tests.

## How TRAE Should Operate Here

When the user asks to operate the meeting bot, TRAE should usually:

1. Check current process state and relevant local `.env` values
2. Apply any requested local runtime tweaks
3. Restart the bot cleanly
4. Confirm readiness from logs
5. Report the active voice and current process status succinctly

When the user asks for future reusability or project memory, update this `AGENTS.md`.
