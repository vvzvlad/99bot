#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import os
from datetime import datetime
from pyrogram import Client
from pyrogram.types import Message
from pyrogram import filters

logger = logging.getLogger(__name__)

class TitleMonitor:
    def __init__(self, client: Client, log_dir: str = "data"):
        self.client = client
        self.log_dir = log_dir
        os.makedirs(self.log_dir, exist_ok=True)
        self.log_file = os.path.join(self.log_dir, "title_changes.log")
        
    def setup_handler(self):
        """Setup handler for title change monitoring"""
        @self.client.on_message(filters.service & ~filters.bot)
        async def monitor_title_changes(client, message: Message):
            await self._handle_title_change(message)
    
    async def _handle_title_change(self, message: Message):
        """Handle title change service message"""
        try:
            # Check if this is a title change message
            if not (message.service and hasattr(message, 'new_chat_title')):
                return
            
            # Get information
            chat_id = message.chat.id
            chat_type = message.chat.type
            new_title = message.new_chat_title
            old_title = message.chat.title if hasattr(message.chat, 'title') else "Unknown"
            
            # Get who changed the title
            changer = "Unknown"
            if message.from_user:
                changer_first = message.from_user.first_name or ""
                changer_last = message.from_user.last_name or ""
                changer_username = message.from_user.username or ""
                changer_id = message.from_user.id
                
                changer = f"{changer_first} {changer_last}".strip()
                if changer_username:
                    changer += f" (@{changer_username})"
                changer += f" [ID: {changer_id}]"
            
            # Format log entry
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = (
                f"[{timestamp}] "
                f"Chat: {old_title} (ID: {chat_id}, Type: {chat_type}) | "
                f"New Title: \"{new_title}\" | "
                f"Changed by: {changer}\n"
            )
            
            # Write to log file
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(log_entry)
                logger.info(f"Title change logged: {chat_id} -> \"{new_title}\"")
            except Exception as e:
                logger.error(f"Failed to write title change log: {str(e)}")
            
            # Delete the service message
            try:
                await message.delete()
                logger.debug(f"Deleted title change service message in {chat_id}")
            except Exception as e:
                logger.warning(f"Failed to delete title change service message: {str(e)}")
                
        except Exception as e:
            logger.error(f"Error in title monitor: {str(e)}", exc_info=True)
