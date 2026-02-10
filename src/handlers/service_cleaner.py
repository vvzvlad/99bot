#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Service Message Cleaner Plugin
Автоматически удаляет служебные сообщения о переименовании чата и смене фото
"""

import logging
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.enums import MessageServiceType

logger = logging.getLogger(__name__)


async def handle_service_message(client: Client, message: Message):
    try:
        logger.info(f"[SERVICE DEBUG] Service message received in chat {message.chat.id}")
        logger.info(f"[SERVICE DEBUG] Message is service: {message.service}")
        logger.info(f"[SERVICE DEBUG] Service type: {message.service if message.service else 'None'}")
        
        if not message.service:
            logger.info(f"[SERVICE DEBUG] Not a service message, skipping")
            return

        if message.service in [
            MessageServiceType.NEW_CHAT_TITLE,
            MessageServiceType.NEW_CHAT_PHOTO,
        ]:
            service_type = "title" if message.service == MessageServiceType.NEW_CHAT_TITLE else "photo"
            logger.info(f"[SERVICE DEBUG] Attempting to delete {service_type} service message")
            await message.delete()
            logger.info(
                f"Deleted service message about {service_type} change in chat {message.chat.id}"
            )
        else:
            logger.info(f"[SERVICE DEBUG] Service type {message.service} not handled")

    except Exception as e:
        logger.error(f"Error deleting service message: {str(e)}", exc_info=True)


def register_handler(client: Client, group: int = 1):
    """
    Регистрирует обработчик служебных сообщений

    Args:
        client: Pyrogram client instance
    """
    @client.on_message(filters.service & filters.group, group=group)
    async def service_cleaner_handler(client: Client, message: Message):
        await handle_service_message(client, message)
        await message.continue_propagation()

    logger.info("Service message cleaner handler registered")
