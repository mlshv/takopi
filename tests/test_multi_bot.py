"""Tests for multi-bot support."""

from __future__ import annotations

from dataclasses import replace

import pytest

from takopi.lockfile import token_fingerprint
from takopi.settings import TelegramTransportSettings
from takopi.telegram.bridge import TelegramBridgeConfig, TelegramTransport
from takopi.telegram.types import (
    TelegramCallbackQuery,
    TelegramIncomingMessage,
)
from takopi.transport import MessageRef, RenderedMessage, SendOptions
from tests.telegram_fakes import FakeBot, FakeTransport, make_cfg


# ---------------------------------------------------------------------------
# Config / settings tests
# ---------------------------------------------------------------------------


class TestTelegramTransportSettings:
    def test_agents_empty_by_default(self):
        settings = TelegramTransportSettings(
            bot_token="primary-token", chat_id=-100123
        )
        assert settings.agents == {}

    def test_agents_loads_correctly(self):
        settings = TelegramTransportSettings(
            bot_token="primary-token",
            chat_id=-100123,
            agents={101: "agent-token-1", 102: "agent-token-2"},
        )
        assert settings.agents == {101: "agent-token-1", 102: "agent-token-2"}

    def test_agents_rejects_primary_duplicate(self):
        with pytest.raises(ValueError, match="must not match the primary"):
            TelegramTransportSettings(
                bot_token="primary-token",
                chat_id=-100123,
                agents={101: "primary-token"},
            )

    def test_agents_rejects_agent_duplicate_tokens(self):
        with pytest.raises(ValueError, match="duplicate of another agent"):
            TelegramTransportSettings(
                bot_token="primary-token",
                chat_id=-100123,
                agents={101: "same-token", 102: "same-token"},
            )

    def test_single_bot_config_unchanged(self):
        settings = TelegramTransportSettings(
            bot_token="primary-token", chat_id=-100123
        )
        assert settings.bot_token == "primary-token"
        assert settings.chat_id == -100123
        assert settings.agents == {}


# ---------------------------------------------------------------------------
# Types tests
# ---------------------------------------------------------------------------


class TestSourceBotFields:
    def test_incoming_message_has_source_bot_fields(self):
        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=42,
            source_bot_key="abc123",
            source_bot_id=999,
        )
        assert msg.source_bot_key == "abc123"
        assert msg.source_bot_id == 999
        assert msg.source_bot is None

    def test_incoming_message_defaults_none(self):
        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=42,
        )
        assert msg.source_bot_key is None
        assert msg.source_bot_id is None
        assert msg.source_bot is None

    def test_callback_query_has_source_bot_fields(self):
        query = TelegramCallbackQuery(
            transport="telegram",
            chat_id=123,
            message_id=1,
            callback_query_id="q1",
            data="test",
            sender_id=42,
            thread_id=101,
            source_bot_key="abc123",
            source_bot_id=999,
        )
        assert query.thread_id == 101
        assert query.source_bot_key == "abc123"
        assert query.source_bot_id == 999

    def test_replace_adds_source_bot(self):
        msg = TelegramIncomingMessage(
            transport="telegram",
            chat_id=123,
            message_id=1,
            text="hello",
            reply_to_message_id=None,
            reply_to_text=None,
            sender_id=42,
        )
        fake_bot = FakeBot()
        updated = replace(
            msg,
            source_bot=fake_bot,
            source_bot_key="key123",
            source_bot_id=999,
        )
        assert updated.source_bot is fake_bot
        assert updated.source_bot_key == "key123"
        assert updated.source_bot_id == 999


# ---------------------------------------------------------------------------
# MessageRef tests
# ---------------------------------------------------------------------------


class TestMessageRefSourceBotKey:
    def test_source_bot_key_field(self):
        ref = MessageRef(
            channel_id=123,
            message_id=1,
            source_bot_key="abc123",
        )
        assert ref.source_bot_key == "abc123"

    def test_source_bot_key_not_in_hash(self):
        ref1 = MessageRef(channel_id=123, message_id=1, source_bot_key="abc")
        ref2 = MessageRef(channel_id=123, message_id=1, source_bot_key="def")
        assert hash(ref1) == hash(ref2)
        assert ref1 == ref2

    def test_source_bot_key_default_none(self):
        ref = MessageRef(channel_id=123, message_id=1)
        assert ref.source_bot_key is None


# ---------------------------------------------------------------------------
# TelegramBridgeConfig tests
# ---------------------------------------------------------------------------


class TestBridgeConfigBotForThread:
    def test_returns_agent_bot_for_known_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        cfg = make_cfg(FakeTransport())
        cfg = replace(cfg, bot=primary, agent_bots={101: agent})
        assert cfg.bot_for_thread(101) is agent

    def test_returns_primary_for_unknown_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        cfg = make_cfg(FakeTransport())
        cfg = replace(cfg, bot=primary, agent_bots={101: agent})
        assert cfg.bot_for_thread(999) is primary

    def test_returns_primary_for_none_thread(self):
        primary = FakeBot()
        cfg = make_cfg(FakeTransport())
        cfg = replace(cfg, bot=primary)
        assert cfg.bot_for_thread(None) is primary


# ---------------------------------------------------------------------------
# TelegramTransport bot resolution tests
# ---------------------------------------------------------------------------


class TestTransportBotResolution:
    def test_resolve_bot_returns_agent_for_known_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(primary, agent_bots={101: agent})
        assert transport._resolve_bot(101) is agent

    def test_resolve_bot_returns_primary_for_unknown_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(primary, agent_bots={101: agent})
        assert transport._resolve_bot(999) is primary

    def test_resolve_bot_returns_primary_for_none(self):
        primary = FakeBot()
        transport = TelegramTransport(primary)
        assert transport._resolve_bot(None) is primary

    def test_resolve_bot_for_ref_uses_source_bot_key(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        ref = MessageRef(channel_id=123, message_id=1, source_bot_key="agent1")
        assert transport._resolve_bot_for_ref(ref) is agent

    def test_resolve_bot_for_ref_uses_sent_bots_map(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        transport._sent_bots[(123, 1)] = "agent1"
        ref = MessageRef(channel_id=123, message_id=1)
        assert transport._resolve_bot_for_ref(ref) is agent

    def test_resolve_bot_for_ref_falls_back_to_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(primary, agent_bots={101: agent})
        ref = MessageRef(channel_id=123, message_id=1, thread_id=101)
        assert transport._resolve_bot_for_ref(ref) is agent

    def test_resolve_bot_for_ref_falls_back_to_primary(self):
        primary = FakeBot()
        transport = TelegramTransport(primary)
        ref = MessageRef(channel_id=123, message_id=1)
        assert transport._resolve_bot_for_ref(ref) is primary

    def test_track_sent_caps_at_limit(self):
        primary = FakeBot()
        transport = TelegramTransport(primary)
        for i in range(2100):
            transport._track_sent(123, i, "key")
        assert len(transport._sent_bots) <= 2048


# ---------------------------------------------------------------------------
# Transport send/edit/delete with multi-bot
# ---------------------------------------------------------------------------


@pytest.mark.anyio
class TestTransportMultiBotOps:
    async def test_send_uses_agent_bot_for_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            agent_bots={101: agent},
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        ref = await transport.send(
            channel_id=123,
            message=RenderedMessage(text="hello", extra={}),
            options=SendOptions(thread_id=101),
        )
        assert ref is not None
        assert len(agent.send_calls) == 1
        assert len(primary.send_calls) == 0
        assert agent.send_calls[0]["message_thread_id"] == 101

    async def test_send_uses_primary_for_unknown_thread(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(primary, agent_bots={101: agent})
        ref = await transport.send(
            channel_id=123,
            message=RenderedMessage(text="hello", extra={}),
            options=SendOptions(thread_id=999),
        )
        assert ref is not None
        assert len(primary.send_calls) == 1
        assert len(agent.send_calls) == 0

    async def test_edit_uses_source_bot_key(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        ref = MessageRef(channel_id=123, message_id=1, source_bot_key="agent1")
        await transport.edit(
            ref=ref,
            message=RenderedMessage(text="edited", extra={}),
        )
        assert len(agent.edit_calls) == 1
        assert len(primary.edit_calls) == 0

    async def test_delete_uses_source_bot_key(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        ref = MessageRef(channel_id=123, message_id=1, source_bot_key="agent1")
        ok = await transport.delete(ref=ref)
        assert ok
        assert len(agent.delete_calls) == 1
        assert len(primary.delete_calls) == 0

    async def test_close_closes_all_bots(self):
        primary = FakeBot()
        agent1 = FakeBot()
        agent2 = FakeBot()
        transport = TelegramTransport(
            primary,
            agent_bots={101: agent1, 102: agent2},
        )
        # FakeBot.close() doesn't track calls, but it shouldn't raise
        await transport.close()

    async def test_send_stores_source_bot_key_on_ref(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            agent_bots={101: agent},
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        ref = await transport.send(
            channel_id=123,
            message=RenderedMessage(text="hello", extra={}),
            options=SendOptions(thread_id=101),
        )
        assert ref is not None
        assert ref.source_bot_key == "agent1"

    async def test_edit_preserves_source_bot_key(self):
        primary = FakeBot()
        agent = FakeBot()
        transport = TelegramTransport(
            primary,
            bot_key_to_client={"prim": primary, "agent1": agent},
        )
        ref = MessageRef(
            channel_id=123, message_id=1, source_bot_key="agent1"
        )
        edited_ref = await transport.edit(
            ref=ref,
            message=RenderedMessage(text="edited", extra={}),
        )
        assert edited_ref is not None
        assert edited_ref.source_bot_key == "agent1"


# ---------------------------------------------------------------------------
# Token fingerprint
# ---------------------------------------------------------------------------


class TestTokenFingerprint:
    def test_deterministic(self):
        fp1 = token_fingerprint("test-token")
        fp2 = token_fingerprint("test-token")
        assert fp1 == fp2

    def test_different_tokens_different_fingerprints(self):
        fp1 = token_fingerprint("token-a")
        fp2 = token_fingerprint("token-b")
        assert fp1 != fp2

    def test_length_is_10(self):
        fp = token_fingerprint("test-token")
        assert len(fp) == 10


# ---------------------------------------------------------------------------
# Dedup key tests
# ---------------------------------------------------------------------------


class TestDedup:
    def test_same_update_id_different_bot_key_not_duplicate(self):
        """Updates with same update_id but different bot_key should NOT be deduped."""
        seen: set[tuple[str | None, int]] = set()
        key1 = ("bot-a", 100)
        key2 = ("bot-b", 100)
        seen.add(key1)
        assert key2 not in seen

    def test_same_bot_key_same_update_id_is_duplicate(self):
        seen: set[tuple[str | None, int]] = set()
        key1 = ("bot-a", 100)
        seen.add(key1)
        assert key1 in seen

    def test_none_bot_key_single_bot_compat(self):
        seen: set[tuple[str | None, int]] = set()
        key1 = (None, 100)
        seen.add(key1)
        assert key1 in seen
        assert (None, 101) not in seen
