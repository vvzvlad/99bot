#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Short Reply Watcher Plugin
Monitors for '/й' replies and responds with '/q' at 10% probability
"""

import logging
import random
from pyrogram import Client, filters
from pyrogram.types import Message

logger = logging.getLogger(__name__)


async def handle_short_reply(client: Client, message: Message):
    try:
        text = (message.text or "").strip().lower()
        if text != "/й":
            return

        if not message.reply_to_message:
            return

        if random.random() > 0.1:
            return

        sent = await client.send_message(
            chat_id=message.chat.id,
            text="/q",
            reply_to_message_id=message.reply_to_message.id,
        )
        await sent.delete()
    except Exception as e:
        logger.error(f"Error in short reply handler: {str(e)}", exc_info=True)


def register_handler(client: Client, group: int = 0):
    """Регистрация обработчика '/й'"""

    @client.on_message(filters.text & filters.group, group=group)
    async def short_reply_wrapper(client: Client, message: Message):
        await handle_short_reply(client, message)
        await message.continue_propagation()

    logger.info("Short reply watcher handler registered")
