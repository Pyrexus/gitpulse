"""Entrypoint: wires everything together and runs the aiohttp server."""

from __future__ import annotations

import asyncio
import contextlib
import signal

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeDefault, LinkPreviewOptions
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

from app.bot.handlers import chats, core, inputs, integrations, replies, stats
from app.bot.keyboards import Keys
from app.config import get_settings
from app.context import AppContext
from app.db import repo as db
from app.db.session import make_engine, make_sessionmaker
from app.github.client import GitHub
from app.log import get_logger, setup_logging
from app.security.crypto import SecretBox
from app.security.signing import CallbackSigner
from app.services.artifacts import ArtifactService
from app.services.hook_sync import sync_hook_urls
from app.telegram.sender import Sender
from app.web.server import build_web_app
from app.workers.processor import Processor
from app.workers.queue import EventQueue

log = get_logger("gitpulse")

COMMANDS = [
    BotCommand(command="start", description="Open the GitPulse panel"),
    BotCommand(command="stats", description="Contribution card: /stats <github-username>"),
]


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    # Order matters: commands → chat events → stats → integrations → replies(media) → free text.
    dp.include_routers(
        core.router, chats.router, stats.router, integrations.router, replies.router, inputs.router
    )
    return dp


async def run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)

    session = None
    if settings.telegram_api_base:
        session = AiohttpSession(
            api=TelegramAPIServer.from_base(settings.telegram_api_base, is_local=True)
        )
    bot = Bot(
        settings.bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML, link_preview=LinkPreviewOptions(is_disabled=True)
        ),
    )
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    box = SecretBox(settings.encryption_key.get_secret_value())
    signer = CallbackSigner(box.subkey("callbacks"))
    gh = GitHub(
        settings.github_api_url,
        settings.github_fallback_token.get_secret_value()
        if settings.github_fallback_token
        else None,
    )

    async def on_gone(chat_id: int) -> None:
        async with sessions() as s, s.begin():
            await db.deactivate_chat(s, chat_id)

    async def on_migrate(old: int, new: int) -> None:
        await chats.migrate(ctx, old, new)

    ctx = AppContext(
        settings=settings,
        bot=bot,
        db=sessions,
        gh=gh,
        box=box,
        signer=signer,
        sender=Sender(on_gone, on_migrate),
        keys=Keys(signer),
    )
    processor = Processor(ctx)
    processor.attach_artifacts(ArtifactService(ctx))
    queue = EventQueue(ctx, processor.handle, settings.worker_concurrency)
    ctx.queue = queue

    dp = build_dispatcher()
    dp["ctx"] = ctx

    me = await bot.get_me()
    ctx.bot_username = me.username or ""
    await bot.set_my_commands(COMMANDS, scope=BotCommandScopeDefault())

    app = build_web_app(ctx)
    polling: asyncio.Task[None] | None = None
    if settings.telegram_mode == "webhook":
        secret = settings.telegram_webhook_secret.get_secret_value() or None
        SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(
            app, path=settings.telegram_webhook_path
        )
        await bot.set_webhook(
            f"{settings.public_url}{settings.telegram_webhook_path}",
            secret_token=secret,
            allowed_updates=dp.resolve_used_update_types(),
            max_connections=80,
        )
    else:
        await bot.delete_webhook(drop_pending_updates=False)

    await queue.start()
    sync_task = asyncio.create_task(sync_hook_urls(ctx))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, settings.host, settings.port).start()
    log.info("started", bot=ctx.bot_username, mode=settings.telegram_mode, port=settings.port)

    if settings.telegram_mode == "polling":
        polling = asyncio.create_task(
            dp.start_polling(
                bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=False
            )
        )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)
    try:
        await stop.wait()
    finally:
        log.info("stopping")
        if polling:
            await dp.stop_polling()
            polling.cancel()
        sync_task.cancel()
        await processor.shutdown()
        await queue.stop()
        await runner.cleanup()
        await gh.close()
        await bot.session.close()
        await engine.dispose()


def main() -> None:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run())


if __name__ == "__main__":
    main()
