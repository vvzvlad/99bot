#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Rename Watcher Plugin
Обработка команды /rename для переименования чата
"""

import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors import ChatAdminRequired, ChatNotModified
from handlers.title_monitor import get_title_monitor

logger = logging.getLogger(__name__)


async def handle_rename(client: Client, message: Message):
    """
    Обработка команды /rename

    Объединяет логику:
    - Извлечение нового названия из сообщения/ответа
    - Валидация и обрезка до 255 символов
    - Удаление командного сообщения
    - Вызов client.set_chat_title()
    - Логирование через title_monitor
    - Обработка ошибок
    """
    try:
        new_title = None

        if message.reply_to_message:
            if message.reply_to_message.text:
                new_title = message.reply_to_message.text
            else:
                await message.delete()
                return
        elif message.text and len(message.text.split(maxsplit=1)) > 1:
            new_title = message.text.split(maxsplit=1)[1]
        else:
            await message.delete()
            return

        if not new_title or not new_title.strip():
            await message.delete()
            return

        if len(new_title) > 255:
            new_title = new_title[:255]

        await message.delete()

        chat_id = message.chat.id
        await client.set_chat_title(chat_id, new_title.strip())
        logger.info(f"Chat {chat_id} renamed to: {new_title}")

        title_monitor = get_title_monitor()
        if title_monitor:
            bot_me = await client.get_me()
            bot_username = bot_me.username or bot_me.first_name or "bot"
            await title_monitor.log_title_change(new_title, bot_username)

    except ChatAdminRequired:
        logger.error(f"Bot lacks admin rights in chat {message.chat.id}")
    except ChatNotModified:
        logger.info(
            f"Chat {message.chat.id} title not modified (already set to same value)"
        )
        await message.delete()
    except Exception as e:
        logger.error(f"Error in rename handler: {str(e)}", exc_info=True)


def register_handler(client: Client, group: int = 0):
    """Регистрация обработчика команды /rename"""

    @client.on_message(filters.command("rename") & filters.group, group=group)
    async def rename_wrapper(client: Client, message: Message):
        await handle_rename(client, message)

    logger.info("Rename watcher handler registered")
