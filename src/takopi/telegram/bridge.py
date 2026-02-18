from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal, cast

from ..logging import get_logger
from ..markdown import MarkdownFormatter, MarkdownParts
from ..progress import ProgressState
from ..runner_bridge import ExecBridgeConfig, RunningTask, RunningTasks
from ..transport import MessageRef, RenderedMessage, SendOptions, Transport
from ..transport_runtime import TransportRuntime
from ..context import RunContext
from ..model import ResumeToken
from ..scheduler import ThreadScheduler
from ..settings import (
    TelegramFilesSettings,
    TelegramTopicsSettings,
    TelegramTransportSettings,
)
from .client import BotClient
from .render import MAX_BODY_CHARS, prepare_telegram, prepare_telegram_multi
from .types import TelegramCallbackQuery, TelegramIncomingMessage

logger = get_logger(__name__)

__all__ = [
    "TelegramBridgeConfig",
    "TelegramPresenter",
    "TelegramTransport",
    "build_bot_commands",
    "handle_callback_cancel",
    "handle_cancel",
    "is_cancel_command",
    "run_main_loop",
    "send_with_resume",
]

CANCEL_CALLBACK_DATA = "takopi:cancel"
CANCEL_MARKUP = {
    "inline_keyboard": [[{"text": "cancel", "callback_data": CANCEL_CALLBACK_DATA}]]
}
CLEAR_MARKUP = {"inline_keyboard": []}


class TelegramPresenter:
    def __init__(
        self,
        *,
        formatter: MarkdownFormatter | None = None,
        message_overflow: str = "trim",
    ) -> None:
        self._formatter = formatter or MarkdownFormatter()
        self._message_overflow = message_overflow

    def render_progress(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        label: str = "working",
    ) -> RenderedMessage:
        parts = self._formatter.render_progress_parts(
            state, elapsed_s=elapsed_s, label=label
        )
        text, entities = prepare_telegram(parts)
        reply_markup = CLEAR_MARKUP if _is_cancelled_label(label) else CANCEL_MARKUP
        return RenderedMessage(
            text=text,
            extra={"entities": entities, "reply_markup": reply_markup},
        )

    def render_final(
        self,
        state: ProgressState,
        *,
        elapsed_s: float,
        status: str,
        answer: str,
    ) -> RenderedMessage:
        parts = self._formatter.render_final_parts(
            state, elapsed_s=elapsed_s, status=status, answer=answer
        )
        if self._message_overflow == "split":
            payloads = prepare_telegram_multi(parts, max_body_chars=MAX_BODY_CHARS)
            text, entities = payloads[0]
            extra = {"entities": entities, "reply_markup": CLEAR_MARKUP}
            if len(payloads) > 1:
                followups = [
                    RenderedMessage(
                        text=followup_text,
                        extra={
                            "entities": followup_entities,
                            "reply_markup": CLEAR_MARKUP,
                        },
                    )
                    for followup_text, followup_entities in payloads[1:]
                ]
                extra["followups"] = followups
            return RenderedMessage(text=text, extra=extra)
        text, entities = prepare_telegram(parts)
        return RenderedMessage(
            text=text,
            extra={"entities": entities, "reply_markup": CLEAR_MARKUP},
        )


def _is_cancelled_label(label: str) -> bool:
    stripped = label.strip()
    if stripped.startswith("`") and stripped.endswith("`") and len(stripped) >= 2:
        stripped = stripped[1:-1]
    return stripped.lower() == "cancelled"


@dataclass(frozen=True, slots=True)
class TelegramBridgeConfig:
    bot: BotClient
    runtime: TransportRuntime
    chat_id: int
    startup_msg: str
    exec_cfg: ExecBridgeConfig
    session_mode: Literal["stateless", "chat"] = "stateless"
    show_resume_line: bool = True
    voice_transcription: bool = False
    voice_max_bytes: int = 10 * 1024 * 1024
    voice_transcription_model: str = "gpt-4o-mini-transcribe"
    voice_transcription_base_url: str | None = None
    voice_transcription_api_key: str | None = None
    forward_coalesce_s: float = 1.0
    media_group_debounce_s: float = 1.0
    allowed_user_ids: tuple[int, ...] = ()
    files: TelegramFilesSettings = field(default_factory=TelegramFilesSettings)
    chat_ids: tuple[int, ...] | None = None
    topics: TelegramTopicsSettings = field(default_factory=TelegramTopicsSettings)
    agent_bots: dict[int, BotClient] = field(default_factory=dict)
    bot_key_to_client: dict[str, BotClient] = field(default_factory=dict)
    primary_bot_key: str | None = None

    def bot_for_thread(self, thread_id: int | None) -> BotClient:
        if thread_id is not None and thread_id in self.agent_bots:
            return self.agent_bots[thread_id]
        return self.bot


_SENT_BOTS_LIMIT = 2048


class TelegramTransport:
    def __init__(
        self,
        bot: BotClient,
        agent_bots: dict[int, BotClient] | None = None,
        *,
        bot_key_to_client: dict[str, BotClient] | None = None,
    ) -> None:
        self._bot = bot
        self._agent_bots = agent_bots or {}
        self._bot_key_to_client = bot_key_to_client or {}
        self._sent_bots: dict[tuple[int, int], str] = {}
        self._sent_bots_order: list[tuple[int, int]] = []

    def _resolve_bot(self, thread_id: int | None) -> BotClient:
        if thread_id is not None and thread_id in self._agent_bots:
            return self._agent_bots[thread_id]
        return self._bot

    def _resolve_bot_for_ref(self, ref: MessageRef) -> BotClient:
        if ref.source_bot_key and ref.source_bot_key in self._bot_key_to_client:
            return self._bot_key_to_client[ref.source_bot_key]
        key = (cast(int, ref.channel_id), cast(int, ref.message_id))
        bot_key = self._sent_bots.get(key)
        if bot_key is not None and bot_key in self._bot_key_to_client:
            return self._bot_key_to_client[bot_key]
        thread_id = cast(int | None, ref.thread_id) if ref.thread_id is not None else None
        return self._resolve_bot(thread_id)

    def _track_sent(self, chat_id: int, message_id: int, bot_key: str | None) -> None:
        if bot_key is None:
            return
        key = (chat_id, message_id)
        self._sent_bots[key] = bot_key
        self._sent_bots_order.append(key)
        while len(self._sent_bots_order) > _SENT_BOTS_LIMIT:
            old = self._sent_bots_order.pop(0)
            self._sent_bots.pop(old, None)

    @staticmethod
    def _extract_followups(message: RenderedMessage) -> list[RenderedMessage]:
        followups = message.extra.get("followups")
        if not isinstance(followups, list):
            return []
        return [item for item in followups if isinstance(item, RenderedMessage)]

    async def _send_followups(
        self,
        *,
        chat_id: int,
        followups: list[RenderedMessage],
        reply_to_message_id: int | None,
        message_thread_id: int | None,
        notify: bool,
        bot: BotClient | None = None,
    ) -> None:
        target_bot = bot or self._bot
        for followup in followups:
            await target_bot.send_message(
                chat_id=chat_id,
                text=followup.text,
                entities=followup.extra.get("entities"),
                parse_mode=followup.extra.get("parse_mode"),
                reply_markup=followup.extra.get("reply_markup"),
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                disable_notification=not notify,
            )

    async def close(self) -> None:
        await self._bot.close()
        for agent_bot in self._agent_bots.values():
            await agent_bot.close()

    async def send(
        self,
        *,
        channel_id: int | str,
        message: RenderedMessage,
        options: SendOptions | None = None,
    ) -> MessageRef | None:
        chat_id = cast(int, channel_id)
        reply_to_message_id: int | None = None
        replace_message_id: int | None = None
        message_thread_id: int | None = None
        notify = True
        if options is not None:
            reply_to_message_id = (
                cast(int, options.reply_to.message_id)
                if options.reply_to is not None
                else None
            )
            replace_message_id = (
                cast(int, options.replace.message_id)
                if options.replace is not None
                else None
            )
            notify = options.notify
            message_thread_id = (
                cast(int | None, options.thread_id)
                if options.thread_id is not None
                else None
            )
        else:
            reply_to_message_id = cast(
                int | None,
                message.extra.get("followup_reply_to_message_id"),
            )
            message_thread_id = cast(
                int | None,
                message.extra.get("followup_thread_id"),
            )
            notify = bool(message.extra.get("followup_notify", True))
        bot = self._resolve_bot(
            cast(int, message_thread_id) if message_thread_id is not None else None
        )
        bot_key: str | None = None
        for k, v in self._bot_key_to_client.items():
            if v is bot:
                bot_key = k
                break
        followups = self._extract_followups(message)
        sent = await bot.send_message(
            chat_id=chat_id,
            text=message.text,
            entities=message.extra.get("entities"),
            parse_mode=message.extra.get("parse_mode"),
            reply_markup=message.extra.get("reply_markup"),
            reply_to_message_id=reply_to_message_id,
            message_thread_id=message_thread_id,
            replace_message_id=replace_message_id,
            disable_notification=not notify,
        )
        if sent is None:
            return None
        if followups:
            await self._send_followups(
                chat_id=chat_id,
                followups=followups,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                notify=notify,
                bot=bot,
            )
        message_id = sent.message_id
        thread_id = (
            sent.message_thread_id
            if sent.message_thread_id is not None
            else message_thread_id
        )
        self._track_sent(chat_id, message_id, bot_key)
        return MessageRef(
            channel_id=chat_id,
            message_id=message_id,
            raw=sent,
            thread_id=thread_id,
            source_bot_key=bot_key,
        )

    async def edit(
        self, *, ref: MessageRef, message: RenderedMessage, wait: bool = True
    ) -> MessageRef | None:
        chat_id = cast(int, ref.channel_id)
        message_id = cast(int, ref.message_id)
        bot = self._resolve_bot_for_ref(ref)
        entities = message.extra.get("entities")
        parse_mode = message.extra.get("parse_mode")
        reply_markup = message.extra.get("reply_markup")
        followups = self._extract_followups(message)
        edited = await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=message.text,
            entities=entities,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            wait=wait,
        )
        if edited is None:
            return ref if not wait else None
        if followups:
            reply_to_message_id = cast(
                int | None, message.extra.get("followup_reply_to_message_id")
            )
            message_thread_id = cast(
                int | None, message.extra.get("followup_thread_id")
            )
            notify = bool(message.extra.get("followup_notify", True))
            await self._send_followups(
                chat_id=chat_id,
                followups=followups,
                reply_to_message_id=reply_to_message_id,
                message_thread_id=message_thread_id,
                notify=notify,
                bot=bot,
            )
        message_id = edited.message_id
        thread_id = (
            edited.message_thread_id
            if edited.message_thread_id is not None
            else ref.thread_id
        )
        return MessageRef(
            channel_id=chat_id,
            message_id=message_id,
            raw=edited,
            thread_id=thread_id,
            source_bot_key=ref.source_bot_key,
        )

    async def delete(self, *, ref: MessageRef) -> bool:
        bot = self._resolve_bot_for_ref(ref)
        return await bot.delete_message(
            chat_id=cast(int, ref.channel_id),
            message_id=cast(int, ref.message_id),
        )


async def send_plain(
    transport: Transport,
    *,
    chat_id: int,
    user_msg_id: int,
    text: str,
    notify: bool = True,
    thread_id: int | None = None,
) -> None:
    reply_to = MessageRef(channel_id=chat_id, message_id=user_msg_id)
    rendered_text, entities = prepare_telegram(MarkdownParts(header=text))
    await transport.send(
        channel_id=chat_id,
        message=RenderedMessage(text=rendered_text, extra={"entities": entities}),
        options=SendOptions(reply_to=reply_to, notify=notify, thread_id=thread_id),
    )


def build_bot_commands(
    runtime: TransportRuntime,
    *,
    include_file: bool = True,
    include_topics: bool = False,
):
    from .commands import build_bot_commands as _build

    return _build(
        runtime,
        include_file=include_file,
        include_topics=include_topics,
    )


def is_cancel_command(text: str) -> bool:
    from .commands import is_cancel_command as _is_cancel_command

    return _is_cancel_command(text)


async def handle_cancel(
    cfg: TelegramBridgeConfig,
    msg: TelegramIncomingMessage,
    running_tasks: RunningTasks,
    scheduler: ThreadScheduler | None = None,
) -> None:
    from .commands import handle_cancel as _handle_cancel

    await _handle_cancel(cfg, msg, running_tasks, scheduler)


async def handle_callback_cancel(
    cfg: TelegramBridgeConfig,
    query: TelegramCallbackQuery,
    running_tasks: RunningTasks,
    scheduler: ThreadScheduler | None = None,
    source_bot: BotClient | None = None,
) -> None:
    from .commands import handle_callback_cancel as _handle_callback_cancel

    await _handle_callback_cancel(cfg, query, running_tasks, scheduler, source_bot=source_bot)


async def send_with_resume(
    cfg: TelegramBridgeConfig,
    enqueue: Callable[
        [
            int,
            int,
            str,
            ResumeToken,
            RunContext | None,
            int | None,
            tuple[int, int | None] | None,
            MessageRef | None,
        ],
        Awaitable[None],
    ],
    running_task: RunningTask,
    chat_id: int,
    user_msg_id: int,
    thread_id: int | None,
    session_key: tuple[int, int | None] | None,
    text: str,
) -> None:
    from .loop import send_with_resume as _send_with_resume

    await _send_with_resume(
        cfg,
        enqueue,
        running_task,
        chat_id,
        user_msg_id,
        thread_id,
        session_key,
        text,
    )


async def run_main_loop(
    cfg: TelegramBridgeConfig,
    poller=None,
    *,
    watch_config: bool | None = None,
    default_engine_override: str | None = None,
    transport_id: str | None = None,
    transport_config: TelegramTransportSettings | None = None,
) -> None:
    from .loop import run_main_loop as _run_main_loop

    if poller is None:
        await _run_main_loop(
            cfg,
            watch_config=watch_config,
            default_engine_override=default_engine_override,
            transport_id=transport_id,
            transport_config=transport_config,
        )
    else:
        await _run_main_loop(
            cfg,
            poller=poller,
            watch_config=watch_config,
            default_engine_override=default_engine_override,
            transport_id=transport_id,
            transport_config=transport_config,
        )
