# Lark CLI

## What It Is

`lark-cli` is the official open-source CLI for the Lark/Feishu platform, maintained by the `larksuite` team. It is designed for both human operators and AI agents.

## Core Positioning

- It is a command-line interface for operating Lark and Feishu services from local scripts, terminals, or AI agent workflows.
- It is not only a thin API wrapper. The project is designed to be agent-friendly, with structured commands, smart defaults, and skill packaging for AI tools.
- The repository positions it as covering eighteen business domains, more than two hundred curated commands, and more than twenty built-in agent skills.

## Main Capabilities

- Messaging and chat operations
- Calendar and scheduling
- Docs, Drive, Wiki, Markdown, Sheets, Slides, and Base
- Tasks, Mail, Contacts, Attendance, Approval, OKR, and Meetings
- Real-time event subscription through `lark-cli event`
- AI-agent-oriented skills such as `lark-calendar`, `lark-im`, `lark-doc`, `lark-event`, `lark-vc`, and `lark-vc-agent`

## Three-Layer Command System

The repo describes three levels of usage:

1. Shortcuts: human-friendly and agent-friendly `+` commands with smart defaults
2. API commands: structured commands mapped to official OpenAPI methods
3. Raw API calls: direct access to platform endpoints when full coverage is needed

This layered design is important because it lets an AI agent choose the simplest safe abstraction first, but still drop down to raw APIs when needed.

## Authentication Model

- `lark-cli config init` configures app credentials
- `lark-cli auth login` performs OAuth login
- `lark-cli auth status` checks current login state
- The tool supports running commands as `user` or `bot`
- Scope management is explicit, and missing scopes can block commands until reauthorized

## Why It Matters For AI Agents

- The repo explicitly describes `lark-cli` as agent-native
- It ships with built-in skills for common Lark domains
- Commands are optimized for structured invocation and high success rates in agent environments
- It supports event consumption, automation, and operational workflows without requiring a custom integration layer for every use case

## Security Notes

The repo includes strong warnings:

- The tool can be invoked by AI agents with real user permissions
- Incorrect prompts or hallucinated actions can lead to data leakage or unintended operations
- Users should keep permissions scoped, avoid weakening default protections, and be careful when exposing the bot in shared environments

## Quick Start

Typical quick start from the repo:

1. Install: `npx @larksuite/cli@latest install`
2. Configure: `lark-cli config init`
3. Login: `lark-cli auth login --recommend`
4. Verify: `lark-cli auth status`

## How This Project Uses It

Only mention this section if the user explicitly asks about this meeting agent. In `lark-meeting-talk-agent`, `lark-cli` is mainly used as an operational side channel rather than the primary audio runtime:

- `lark-cli auth login` for user authorization and scope setup
- `lark-cli event consume` for current-meeting-scoped event streaming
- Meeting-related event and artifact workflows that enrich meeting memory

The bot itself still joins meetings through the project runtime and Feishu VC APIs, while `lark-cli` provides authentication and event-driven context support.
