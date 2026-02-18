# Multi-Bot Support for Takopi — Implementation Plan

> Reviewed via dual-review (Claude + Codex). 3 rounds, 15 issues raised, 14 accepted, 1 rejected. All incorporated below.

## Context

FWA (~/code/fwa) is a multi-agent orchestration system where each agent needs its own Telegram bot account so agents can @tag each other in forum topics. Takopi currently supports only a single bot. This fork (github.com/mlshv/takopi) adds multi-bot support.

Upstream: github.com/banteg/takopi (v0.21.4)

## Goal

Single Takopi instance manages N Telegram bots (one per FWA agent), each mapped to a specific forum topic via thread_id. Backward compatible with existing single-bot configs.

## Config Format

### New (multi-agent):
```toml
[transports.telegram]
bot_token = "primary-token"     # Primary bot — handles General topic + fallback
chat_id = -100123456789

[transports.telegram.agents]
# topic thread_id → bot_token mapping
101 = "token-for-backlog-bot"
102 = "token-for-builder-bot"
103 = "token-for-analyst-bot"
```

### Old (unchanged, still works):
```toml
[transports.telegram]
bot_token = "single-token"
chat_id = -100123456789
```

When `agents` is absent or empty, behavior is identical to current single-bot mode.

## Files to Modify (10 files, ~350 lines, 0 new files)

All paths relative to `src/takopi/`.

### 1. `settings.py` — Config schema

- Add `agents: dict[int, NonEmptyStr] = Field(default_factory=dict)` to `TelegramTransportSettings` (after line 109)
- Add `@model_validator` to reject duplicate tokens (primary must not appear in agents, agent tokens must be unique)
- `require_telegram()` unchanged — returns primary token only

### 2. `telegram/types.py` — Source bot tracking

- Add to `TelegramIncomingMessage`:
  - `source_bot_key: str | None = None` — token fingerprint, stable even if get_me() fails
  - `source_bot_id: int | None = None` — telegram user id of the bot
  - `source_bot: BotClient | None = None` — reference to actual bot client (use TYPE_CHECKING import)
- Add to `TelegramCallbackQuery`:
  - Same three fields
  - Add `thread_id: int | None = None` — extract from callback message payload for fallback routing

### 3. `transport.py` — MessageRef bot tracking

- Add `source_bot_key: str | None = None` to `MessageRef` as `field(compare=False, hash=False)`
- Set when TelegramTransport.send() creates a MessageRef
- Used by edit()/delete() to route through correct bot

### 4. `telegram/bridge.py` — Transport + config changes

**TelegramBridgeConfig (line 116-134):**
- Add `agent_bots: dict[int, TelegramClient] = field(default_factory=dict)` — topic thread_id → client
- Add helper: `bot_for_thread(thread_id: int | None) → BotClient`

**TelegramTransport (line 137-298):**
- `__init__`: accept `agent_bots: dict[int, BotClient]`
- Add `_resolve_bot(thread_id: int | None) → BotClient` — returns agent bot or primary
- Add internal `_sent_bots: dict[tuple[int, int], str]` map — `(chat_id, message_id) → bot_key`, capped at 2048 entries
- `_resolve_bot_for_ref(ref: MessageRef) → BotClient` — resolution chain:
  1. `ref.source_bot_key` → lookup in bot registry
  2. Internal `_sent_bots` map by `(channel_id, message_id)`
  3. `_resolve_bot(ref.thread_id)` — thread_id fallback
  4. Primary bot
- `send()`: resolve bot from thread_id, use it for send, store on returned MessageRef, populate `_sent_bots`, pass bot to `_send_followups`
- `edit()`: use `_resolve_bot_for_ref(ref)`, pass to `_send_followups`
- `delete()`: use `_resolve_bot_for_ref(ref)`
- `_send_followups()`: accept explicit `bot` parameter instead of using `self._bot`
- `close()`: close primary + all agent bots

### 5. `telegram/backend.py` — Initialization

**`build_and_run()` (lines 102-159):**
- Create `TelegramClient(agent_token)` for each entry in `settings.agents`
- Build `agent_bots: dict[int, TelegramClient]` (topic_id → client)
- Pass `agent_bots` to both `TelegramTransport(bot, agent_bots=agent_bots)` and `TelegramBridgeConfig(agent_bots=agent_bots)`
- `lock_token()` unchanged — primary token only

### 6. `telegram/loop.py` — Polling + dispatch (biggest change)

**Startup (in `run_main_loop`, ~line 1042-1054):**
- Call `get_me()` on all bots (primary + agents)
- Build `bot_usernames: dict[str, str]` — `bot_key → username` (replaces single `state.bot_username`)
- Assign stable `bot_key` per bot using `lockfile.token_fingerprint(token)` — works even if `get_me()` fails
- Build `bot_key_to_client: dict[str, TelegramClient]` for lookups

**`poll_updates()` (lines 315-330):**
- Single-bot mode: unchanged path
- Multi-bot mode: fan-in via `anyio.create_memory_object_stream`:
  ```python
  send, receive = anyio.create_memory_object_stream[TelegramIncomingUpdate](256)
  async with anyio.create_task_group() as tg:
      async def poll_single(bot, offset, send_clone, bot_key, bot_id):
          async with send_clone:
              async for msg in poll_incoming(bot, chat_ids=..., offset=offset):
                  msg = replace(msg, source_bot=bot, source_bot_key=bot_key, source_bot_id=bot_id)
                  await send_clone.send(msg)

      for bot_key, (bot, offset, bot_id) in all_bots.items():
          tg.start_soon(poll_single, bot, offset, send.clone(), bot_key, bot_id)
      await send.aclose()  # close parent; pollers hold clones

      async with receive:
          async for update in receive:
              yield update
  ```
- Agent bot pollers filter `chat_ids` to `{cfg.chat_id}` only (no private chats)
- Add `_drain_backlog_for_bot()` helper — same as `_drain_backlog` but no startup message

**`route_update()` dedup (~line 1801):**
- Multi-bot: key on `(source_bot_key, update_id)` instead of bare `update_id`
- `state.seen_update_ids` type unchanged (set of tuples works); single-bot mode uses `(None, update_id)`

**`route_update()` callbacks (~line 1833-1847):**
- Use `update.source_bot.answer_callback_query()` instead of `cfg.bot`
- Pass `source_bot` to `handle_callback_cancel()`

**`route_message()` (~line 1576):**
- Voice transcription (~line 1673): use `msg.source_bot or cfg.bot` for downloads
- File operations: thread `msg.source_bot` to file transfer handlers
- Mentions (~line 1664): check source bot's username specifically via `bot_usernames[msg.source_bot_key]`

**`state.bot_username` → `state.bot_usernames`:**
- Type: `dict[str, str]` (bot_key → username)
- `should_trigger_run()`: check against source bot's username as primary, all usernames as fallback

### 7. `telegram/topics.py` — Validation

**`_validate_topics_setup()` (line 194):**
- After validating primary bot, loop over `cfg.agent_bots` and validate each has admin + manage_topics permission

### 8. `telegram/commands/cancel.py` — Callback fixes

- `handle_cancel()` and `handle_callback_cancel()`: accept optional `source_bot` parameter
- Use `source_bot or cfg.bot` for all `answer_callback_query` calls
- When building MessageRef for progress message edit, set `source_bot_key` from lookup

### 9. `telegram/commands/file_transfer.py` — File operations

- Accept optional `source_bot` parameter for handlers
- Use `source_bot or cfg.bot` for ALL bot API calls: `get_file()`, `download_file()`, `send_document()`, `send_message()`

### 10. `telegram/commands/handlers.py` — Command menu

- `set_command_menu()`: register commands for all bots (primary + agents), not just primary

## Key Design Decisions

1. **Bot resolution chain** (edit/delete): `ref.source_bot_key` → transport internal sent_bots map → thread_id fallback → primary bot
2. **Dedup namespace**: `(bot_key, update_id)` where bot_key = token_fingerprint — always available, no get_me() dependency
3. **Agent DMs**: Agent pollers filter to supergroup chat_id only — private chat messages ignored
4. **Fan-in lifecycle**: Each poller gets cloned send stream, closes it in finally. Parent send closed after spawn. Receive yields until all clones closed.
5. **Lightweight bot identity**: `bot_key: str` (token fingerprint) stored in MessageRef and types. BotClient object lookup in Telegram-specific code only.
6. **Mention routing**: Check source bot's username specifically to avoid cross-bot trigger races
7. **Process lock**: Primary token only — lock is per-config, not per-token. Two configs with different primaries are different instances.

## Implementation Order

1. Config + types (`settings.py`, `telegram/types.py`, `transport.py`)
2. Transport plumbing (`telegram/bridge.py`) — including internal bot resolution map
3. Backend init (`telegram/backend.py`)
4. Polling + dispatch (`telegram/loop.py`)
5. Command fixes (`telegram/commands/cancel.py`, `file_transfer.py`, `handlers.py`)
6. Validation (`telegram/topics.py`)
7. Tests

## Testing Checklist

- [ ] Old single-bot config loads and works unchanged
- [ ] Multi-bot config with agents dict loads correctly
- [ ] Duplicate token validation rejects
- [ ] Each agent bot polls its own updates
- [ ] Updates dedup correctly across bots (no false drops)
- [ ] Messages in agent topic → agent bot responds
- [ ] Messages in unbound topic → primary bot responds
- [ ] Cancel/edit of progress message uses correct bot
- [ ] File upload/download in agent topic uses source bot
- [ ] @mention of agent bot triggers run
- [ ] @mention of different agent bot doesn't trigger on wrong poller
- [ ] Callback queries answered by correct bot
- [ ] All bots closed on shutdown
- [ ] get_me() failure for one bot doesn't break others

## Edge Cases

- **Rate limiting**: Per-TelegramClient outbox — each bot has independent Telegram rate limits
- **Bot file access**: Only source_bot can access its files — source_bot field ensures this
- **Config hot-reload**: Agent token changes require restart (same as primary token)
- **Non-topic messages to agent bots**: Filtered out by chat_ids restriction

## Codex Review Summary

3 rounds of review. Key issues caught and resolved:
- Cross-bot update_id collision (dedup by bot_key)
- Callback routing to wrong bot (source_bot propagation)
- File operations incomplete (all API calls routed)
- MessageRef reconstruction losing bot identity (internal sent_bots map)
- Fan-in shutdown semantics (clone/close lifecycle)
- Mention trigger races (source bot username check)
- get_me() failure breaking dedup (token fingerprint as stable key)
