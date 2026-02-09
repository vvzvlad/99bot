#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
from pyrogram import Client
from pyrogram.types import Message

logger = logging.getLogger(__name__)

class ChatManager:
    def __init__(self, client: Client):
        self.client = client

    async def rename_chat(self, message: Message, new_title: str) -> bool:
        """
        Rename chat title
        
        Args:
            message: Message object containing chat info
            new_title: New chat title
            
        Returns:
            True if successful, False otherwise
        """
        try:
            chat_id = message.chat.id
            # Rename chat
            await self.client.set_chat_title(chat_id, new_title)
            logger.info(f"Chat {chat_id} renamed to: {new_title}")
            
            # Delete service message about title change
            async for msg in self.client.get_chat_history(chat_id, limit=10):
                if msg.service and hasattr(msg, 'new_chat_title'):
                    try:
                        await msg.delete()
                        logger.debug(f"Deleted service message about title change in {chat_id}")
                    except Exception as e:
                        logger.warning(f"Failed to delete service message: {str(e)}")
                    break
            
            return True
        except Exception as e:
            logger.error(f"Failed to rename chat {message.chat.id}: {str(e)}")
            raise

    async def set_chat_photo(self, message: Message, photo_path: str) -> bool:
        """
        Set chat photo
        
        Args:
            message: Message object containing chat info
            photo_path: Path to photo file
            
        Returns:
            True if successful, False otherwise
        """
        try:
            if not os.path.exists(photo_path):
                logger.error(f"Photo file not found: {photo_path}")
                return False
                
            chat_id = message.chat.id
            # Set chat photo
            await self.client.set_chat_photo(chat_id, photo=photo_path)
            logger.info(f"Chat {chat_id} photo updated with: {photo_path}")
            
            # Delete service message about photo change
            async for msg in self.client.get_chat_history(chat_id, limit=10):
                if msg.service and hasattr(msg, 'new_chat_photo'):
                    try:
                        await msg.delete()
                        logger.debug(f"Deleted service message about photo change in {chat_id}")
                    except Exception as e:
                        logger.warning(f"Failed to delete service message: {str(e)}")
                    break
            
            return True
        except Exception as e:
            logger.error(f"Failed to set chat photo for {message.chat.id}: {str(e)}")
            raise
        finally:
            # Cleanup temp file
            if os.path.exists(photo_path):
                try:
                    os.remove(photo_path)
                    logger.debug(f"Temp photo file removed: {photo_path}")
                except Exception as e:
                    logger.warning(f"Failed to remove temp file {photo_path}: {str(e)}")
