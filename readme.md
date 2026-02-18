# takopi (multi-bot fork)

Fork of [banteg/takopi](https://github.com/banteg/takopi) with multi-bot support.

Upstream takopi manages a single Telegram bot per instance. This fork allows a single instance to manage N bots, each mapped to a specific forum topic via `thread_id`. Built for [FWA](https://github.com/mlshv/fwa), a multi-agent orchestration system where each agent needs its own Telegram bot account so agents can @tag each other in forum topics.

## What's different

A single takopi instance can now run multiple Telegram bots. Each bot is mapped to a forum topic thread. Messages in a topic are handled by the corresponding bot; messages in unbound topics fall back to the primary bot. Everything else (projects, worktrees, resume, plugins) works the same as upstream.

### Multi-bot config

```toml
[transports.telegram]
bot_token = "primary-token"     # primary bot - handles General topic + fallback
chat_id = -100123456789

[transports.telegram.agents]
# topic thread_id -> bot_token mapping
101 = "token-for-backlog-bot"
102 = "token-for-builder-bot"
103 = "token-for-analyst-bot"
```

When `agents` is absent or empty, behavior is identical to upstream single-bot mode. Existing configs work without changes.

### How it works

- Each agent bot polls its own updates independently (fan-in via anyio streams)
- Updates are deduped per-bot using `(bot_key, update_id)` tuples, so cross-bot update ID collisions don't cause false drops
- Bot resolution for send/edit/delete follows a chain: message's `source_bot_key` -> internal sent-message map -> thread_id lookup -> primary bot fallback
- File operations (upload/download), voice transcription, and callback queries all route through the correct source bot
- @mention routing checks the source bot's username specifically to avoid cross-bot trigger races
- Duplicate tokens (primary appearing in agents, or agent tokens repeated) are rejected at config validation

---

*Everything below is from upstream.*

---

## features

- projects and worktrees: work on multiple repos/branches simultaneously, branches are git worktrees
- stateless resume: continue in chat or copy the resume line to pick up in terminal
- progress streaming: commands, tools, file changes, elapsed time
- parallel runs across agent sessions, per-agent-session queue
- works with telegram features like voice notes and scheduled messages
- file transfer: send files to the repo or fetch files/dirs back
- group chats and topics: map group topics to repo/branch contexts
- works with existing anthropic and openai subscriptions

## requirements

`uv` for installation (`curl -LsSf https://astral.sh/uv/install.sh | sh`)

python 3.14+ (`uv python install 3.14`)

at least one engine on PATH: `codex`, `claude`, `opencode`, or `pi`

## install

```sh
uv tool install -U takopi
```

## setup

run `takopi` and follow the setup wizard. it will help you:

1. create a bot token via @BotFather
2. pick a workflow (assistant, workspace, or handoff)
3. connect your chat
4. choose a default engine

workflows configure conversation mode, topics, and resume lines automatically:

- **assistant**: ongoing chat with auto-resume (recommended)
- **workspace**: forum topics bound to repos/branches
- **handoff**: reply-to-continue with terminal resume lines

## usage

```sh
cd ~/dev/happy-gadgets
takopi
```

send a message to your bot. prefix with `/codex`, `/claude`, `/opencode`, or `/pi` to pick an engine. reply to continue a thread.

register a project with `takopi init happy-gadgets`, then target it from anywhere with `/happy-gadgets hard reset the timeline`.

mention a branch to run an agent in a dedicated worktree `/happy-gadgets @feat/memory-box freeze artifacts forever`.

inspect or update settings with `takopi config list`, `takopi config get`, and `takopi config set`.

see [takopi.dev](https://takopi.dev/) for configuration, worktrees, topics, file transfer, and more.

## plugins

takopi supports entrypoint-based plugins for engines, transports, and commands.

see [`docs/how-to/write-a-plugin.md`](docs/how-to/write-a-plugin.md) and [`docs/reference/plugin-api.md`](docs/reference/plugin-api.md).

## development

see [`docs/reference/specification.md`](docs/reference/specification.md) and [`docs/developing.md`](docs/developing.md).
