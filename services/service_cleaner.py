#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
from pyrogram import Client
from pyrogram.types import Message
from pyrogram.enums import MessageServiceType

logger = logging.getLogger(__name__)

# Global instance
_instance = None

def get_service_cleaner():
    """Get the global ServiceMessageCleaner instance"""
    return _instance

def set_service_cleaner(cleaner):
    """Set the global ServiceMessageCleaner instance"""
    global _instance
    _instance = cleaner


class ServiceMessageCleaner:
    """Cleaner for removing service messages about chat title/photo changes"""

    def __init__(self):
        self.deleted_count = 0

    async def handle_service_message(self, client: Client, message: Message):
        """
        Handle service messages and remove chat title/photo change notifications

        Args:
            client: Pyrogram client instance
            message: Message object containing service message
        """
        try:
            if not message.service:
                return

            if message.service == MessageServiceType.NEW_CHAT_TITLE:
                await message.delete()
                self.deleted_count += 1
                logger.info(
                    f"Deleted service message: type=title, chat_id={message.chat.id}"
                )
                return

            if message.service == MessageServiceType.NEW_CHAT_PHOTO:
                await message.delete()
                self.deleted_count += 1
                logger.info(
                    f"Deleted service message: type=photo, chat_id={message.chat.id}"
                )
                return
        except Exception as e:
            logger.error(
                f"Error handling service message deletion: {str(e)}",
                exc_info=True
            )
